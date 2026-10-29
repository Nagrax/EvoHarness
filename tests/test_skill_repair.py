"""SKILL.md 一键修复回归测试：建议生成 + 应用 + 备份 + 验证 + 回滚。

覆盖 agents/skill_repair.py 的三类修复策略：
- encoding：GBK 坏编码 → UTF-8 转码（auto_safe，无语义风险）
- colon：frontmatter 中文冒号 → 英文冒号 + 补空格（CRLF 归一化，正文不动）
- unclosed：frontmatter 未闭合 → 启发式补闭合线（歧义时不出建议）

应用链路三件套：.bak 备份（不覆盖已有备份）→ 重写（LF 统一，禁 Windows 再转
CRLF）→ 重新 _parse_skill_file 验证，失败自动回滚。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.skill_repair import (
    _fix_chinese_colons,
    _fix_unclosed_frontmatter,
    _verify_current_state,
    apply_repair,
    build_repair_suggestion,
)


def test_colon_fix_adds_space_and_keeps_body():
    text = "---\nname：好技能\ndescription： 测试\n---\n正：文不动"
    fixed = _fix_chinese_colons(text)
    assert "name: 好技能" in fixed
    assert "description: 测试" in fixed          # 已有空格不重复加
    assert "正：文不动" in fixed                  # 正文不动


def test_colon_fix_normalizes_crlf():
    text = "---\r\nname：好技能\r\n---\r\n\r\n正文"
    fixed = _fix_chinese_colons(text)
    assert "\r" not in fixed
    assert fixed.startswith("---\nname: 好技能\n---\n\n正文")


def test_unclosed_fix_inserts_separator():
    text = "---\nname: x\ndescription: y\n这里开始是正文"
    fixed = _fix_unclosed_frontmatter(text)
    assert fixed is not None
    assert fixed.split("\n")[3] == "---"          # 在首个非字段行前闭合
    assert "这里开始是正文" in fixed


def test_unclosed_ambiguous_returns_none():
    assert _fix_unclosed_frontmatter("---\nname: x") is None


def test_suggest_and_apply_encoding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    p = tmp_path / "enc/SKILL.md"
    p.parent.mkdir()
    p.write_bytes("---\nname: test\n---\n正文".encode("gbk"))

    s = build_repair_suggestion(str(p), "load_failed", "'utf-8' codec can't decode byte")
    assert s["available"] and s["kind"] == "encoding" and s["auto_safe"]

    r = apply_repair(str(p), "encoding")
    assert r["ok"] and Path(r["backup"]).exists()
    assert p.read_text(encoding="utf-8").startswith("---\nname: test")


def test_suggest_and_apply_colon_full_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    p = tmp_path / "colon/SKILL.md"
    p.parent.mkdir()
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("---\r\nname：好技能\r\ndescription：测试\r\n---\r\n\r\n正文。")

    reason = "中文冒号导致字段被跳过: name：好技能"
    s = build_repair_suggestion(str(p), "load_warning", reason)
    assert s["available"] and s["kind"] == "colon" and not s["auto_safe"]
    assert "+name: 好技能" in s["diff"] and "-name：好技能" in s["diff"]

    r = apply_repair(str(p), "colon")
    assert r["ok"], r
    raw = p.read_bytes()
    assert b"\r" not in raw                       # CRLF 已归一
    fixed = raw.decode("utf-8")
    assert "name: 好技能" in fixed and "正文。" in fixed


def test_apply_colon_on_non_utf8_file_does_not_leave_backup(tmp_path, monkeypatch):
    """回归：GBK 文件按 colon 修复时 utf-8 解码抛 UnicodeDecodeError——不得落半成品
    .bak（修复前 .bak 已写、原文件未改），需明确报错且原文件保持不变。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    p = tmp_path / "gbk/SKILL.md"
    p.parent.mkdir()
    original = "---\nname：test\n---\n正文".encode("gbk")
    p.write_bytes(original)

    r = apply_repair(str(p), "colon")
    assert r["ok"] is False and r.get("error")
    assert p.read_bytes() == original                       # 原文件未改
    assert not p.with_suffix(".md.bak").exists()            # 未落半成品备份


def test_apply_unclosed_on_non_utf8_file_does_not_leave_backup(tmp_path, monkeypatch):
    # 同上：unclosed 路径对 GBK 文件同样不得落半成品 .bak。
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    p = tmp_path / "gbk2/SKILL.md"
    p.parent.mkdir()
    original = "---\nname：test\n这里正文".encode("gbk")
    p.write_bytes(original)

    r = apply_repair(str(p), "unclosed")
    assert r["ok"] is False and r.get("error")
    assert p.read_bytes() == original
    assert not p.with_suffix(".md.bak").exists()


def test_apply_backup_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    p = tmp_path / "c/SKILL.md"
    p.parent.mkdir()
    p.write_text("---\nname：a\n---\nx", encoding="utf-8")
    assert apply_repair(str(p), "colon")["ok"]
    bak = p.with_suffix(".md.bak")
    first = bak.read_bytes()
    # 二次修复（人为再写坏）不覆盖第一份备份
    p.write_text("---\nname：b\n---\ny", encoding="utf-8")
    assert apply_repair(str(p), "colon")["ok"]
    assert bak.read_bytes() == first


def test_suggest_unavailable_for_missing_file(tmp_path):
    s = build_repair_suggestion(str(tmp_path / "nope/SKILL.md"), "load_failed", "codec")
    assert not s["available"]


def test_mixed_colon_line_repairable_and_consistent(tmp_path, monkeypatch):
    """回归：混合冒号行（description: 说明：xxx）此前口径矛盾——_fix_chinese_colons 只处理
    "有中文冒号且无英文冒号"的行，故 build_repair_suggestion 谎称"已修复"（available=False），
    而 _verify_current_state 仍判 repairable，UI 据此隐藏仍损坏文件的修复入口。

    修复后 frontmatter 内全部中文冒号被消除，两处口径一致（可修 → 修好后 fixed）。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".evoharness" / "skill-evolution").mkdir(parents=True)
    p = tmp_path / "mixed/SKILL.md"
    p.parent.mkdir()
    p.write_text("---\nname: 好技能\ndescription: 说明：这是值\n---\n\n正文。", encoding="utf-8")
    reason = "中文冒号导致字段被跳过: description: 说明：这是值"

    s = build_repair_suggestion(str(p), "load_warning", reason)
    assert s["available"] and s["kind"] == "colon"
    assert "description: 说明: 这是值" in s["fixed_preview"]
    assert _verify_current_state(str(p), "colon") == "repairable"

    r = apply_repair(str(p), "colon")
    assert r["ok"], r
    fixed = p.read_text(encoding="utf-8")
    assert "：" not in fixed
    assert "description: 说明: 这是值" in fixed
    # 修好后两处口径仍一致：不可再出建议 + state=fixed
    assert _verify_current_state(str(p), "colon") == "fixed"
    assert build_repair_suggestion(str(p), "load_warning", reason)["available"] is False
