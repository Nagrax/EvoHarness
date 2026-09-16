"""加载层溯源回归测试：SKILL.md 解析失败不再静默消失。

背景：_parse_skill_file 原为 except Exception: return None——SKILL.md 写坏（GBK 编码、
front matter 格式错）时 skill 无声消失，无任何日志（2026-09-16 名创面试复盘定位的排障
盲区）。现改为两类溯源记录，落盘到 .evoharness/skill-evolution/skill_load_errors.jsonl：
  - load_failed：解析抛异常（编码错/文件坏），skill 不加载；
  - load_warning：不抛异常但行为静默变坏——front matter 未闭合（meta 全空、全文进正文）、
    中文冒号导致字段被 frontmatter.py 的 find(":") 整行跳过（元数据丢失、检索精度下降）。
本文件固化该溯源链，并防止误报（合法 skill 不产生记录）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skills import _parse_skill_file

ERRORS_PATH = Path(".evoharness") / "skill-evolution" / "skill_load_errors.jsonl"


def _recorded(tmp_path: Path) -> list[dict]:
    path = tmp_path / ERRORS_PATH
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_load_failed_on_gbk_encoding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / "gbk-skill"
    skill_dir.mkdir()
    # "中文" 的 GBK 字节：utf-8-sig 读取必然抛 UnicodeDecodeError
    (skill_dir / "SKILL.md").write_bytes("中文内容".encode("gbk"))

    assert _parse_skill_file(skill_dir / "SKILL.md", "project", str(skill_dir)) is None

    rows = _recorded(tmp_path)
    assert any(r["event"] == "load_failed" and r["error_type"] == "UnicodeDecodeError" for r in rows)


def test_load_warning_on_chinese_colon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / "colon-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: colon-skill\n"
        "description：中文冒号写错\n"
        "---\n\n"
        "# 正文\n说明内容\n",
        encoding="utf-8",
    )

    skill = _parse_skill_file(skill_dir / "SKILL.md", "project", str(skill_dir))
    # skill 仍会加载（name 来自 front matter），但 description 被静默丢弃
    assert skill is not None and skill.name == "colon-skill"
    assert skill.description == ""

    rows = _recorded(tmp_path)
    assert any(
        r["event"] == "load_warning" and "中文冒号" in r["reason"] and "description" in r["reason"]
        for r in rows
    )


def test_load_warning_on_unclosed_frontmatter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / "unclosed-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: unclosed-skill\n"
        "description: 没有闭合分隔符\n"
        "正文直接开始\n",
        encoding="utf-8",
    )

    skill = _parse_skill_file(skill_dir / "SKILL.md", "project", str(skill_dir))
    # meta 全空，name 回退目录名，front matter 原文混入正文
    assert skill is not None and skill.name == "unclosed-skill"

    rows = _recorded(tmp_path)
    assert any(r["event"] == "load_warning" and "未闭合" in r["reason"] for r in rows)


def test_valid_skill_records_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / "valid-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: valid-skill\n"
        "description: 合法 skill\n"
        "---\n\n"
        "# 正文\n正常内容\n",
        encoding="utf-8",
    )

    skill = _parse_skill_file(skill_dir / "SKILL.md", "project", str(skill_dir))
    assert skill is not None and skill.description == "合法 skill"
    assert _recorded(tmp_path) == []
