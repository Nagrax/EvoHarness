"""SKILL.md 加载异常的建议修复与一键应用。

修复策略按语义风险分级（与评测体系同一哲学：确定性变换可全自动，
含语义判断的只出建议、由人确认）：
- encoding（GBK 等坏编码）：字节级转码，零语义风险；
- colon（frontmatter 中文冒号）：机械替换 ：→ :，字段归属展示给人看；
- unclosed（frontmatter 未闭合）：在正文前补闭合线，位置有歧义，必须人工确认。

应用流程统一三件套：备份原文件（.bak 后缀，不覆盖已有备份）→ 写入修复
内容 → 重新 _parse_skill_file 验证（解析成功且不再触发本次异常才算修好）。
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _decode_gbk(raw: bytes) -> str | None:
    """尝试以 GBK 家族解码；失败返回 None。"""
    for enc in ("gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def build_repair_suggestion(file_str: str, event: str, reason: str) -> dict[str, Any]:
    """根据一条 load_error 记录生成建议修复。无建议时 returns {'available': False}。"""
    path = Path(file_str)
    if not path.is_file():
        return {"available": False, "why": "文件已不存在（可能已被删除或重命名）"}

    raw_bytes = _read_bytes(path)
    kind = _classify(event, reason)

    if kind == "encoding":
        text = _decode_gbk(raw_bytes)
        if text is None:
            return {"available": False, "why": "无法以 GBK 家族解码，需人工检查文件"}
        fixed = text
        action = "转换为 UTF-8 编码（内容不变）"
        auto_safe = True
    elif kind == "colon":
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return {"available": False, "why": "文件同时存在编码问题，先修编码"}
        fixed = _fix_chinese_colons(text)
        if fixed == text:
            return {"available": False, "why": "未再检测到中文冒号（可能已修复）"}
        action = "frontmatter 字段的中文冒号 ： 替换为英文 :"
        auto_safe = False
    elif kind == "unclosed":
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return {"available": False, "why": "文件同时存在编码问题，先修编码"}
        fixed = _fix_unclosed_frontmatter(text)
        if fixed is None:
            return {"available": False, "why": "无法确定闭合线位置，需人工检查"}
        action = "在 frontmatter 后补闭合分隔线 ---"
        auto_safe = False
    else:
        return {"available": False, "why": "该异常类型暂不支持自动修复建议"}

    if kind == "encoding":
        old_lines = ["（原文件为 GBK 编码，以下为转码后内容）"]
    else:
        old_lines = text.splitlines()
    diff = "\n".join(difflib.unified_diff(
        old_lines, fixed.splitlines(),
        fromfile="当前", tofile="修复后", lineterm=""))
    return {
        "available": True,
        "kind": kind,
        "action": action,
        "auto_safe": auto_safe,
        "diff": diff[:8000],
        "fixed_preview": fixed[:4000],
    }


def apply_repair(file_str: str, kind: str) -> dict[str, Any]:
    """应用修复：备份 → 重写 → 重新解析验证。失败自动回滚。"""
    path = Path(file_str)
    if not path.is_file():
        return {"ok": False, "error": "文件已不存在"}

    raw_bytes = _read_bytes(path)
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        backup.write_bytes(raw_bytes)

    if kind == "encoding":
        fixed = _decode_gbk(raw_bytes)
        if fixed is None:
            return {"ok": False, "error": "解码失败，未修改文件"}
        fixed = _normalize_newlines(fixed)
    elif kind == "colon":
        fixed = _fix_chinese_colons(raw_bytes.decode("utf-8-sig"))
    elif kind == "unclosed":
        fixed = _fix_unclosed_frontmatter(raw_bytes.decode("utf-8-sig"))
        if fixed is None:
            return {"ok": False, "error": "无法定位闭合位置，未修改文件"}
    else:
        return {"ok": False, "error": "未知修复类型"}

    # newline="\n"：内容已统一 LF，禁止 Windows 文本模式再转 CRLF
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(fixed)

    # 重新走真实解析器验证：解析成功 + 本次异常类别不再触发，才算修好
    verified, err = _verify_repaired(path, kind)
    if not verified:
        backup_text = backup.read_bytes()
        path.write_bytes(backup_text)
        return {"ok": False, "error": f"验证未通过已回滚：{err}", "rolled_back": True}
    return {"ok": True, "backup": str(backup)}


def _verify_repaired(path: Path, kind: str) -> tuple[bool, str]:
    from .frontmatter import parse_frontmatter
    from .skills import _parse_skill_file

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        return False, f"编码仍不可读: {e}"
    result = parse_frontmatter(raw)
    if kind in ("colon", "unclosed") and not result.meta:
        return False, "frontmatter 元数据仍为空"
    skill = _parse_skill_file(path, source="repair-verify", skill_dir=str(path.parent))
    if skill is None:
        return False, "_parse_skill_file 仍返回 None"
    if kind == "colon" and "：" in _frontmatter_block(raw):
        return False, "frontmatter 仍含中文冒号"
    return True, ""


def _frontmatter_block(raw: str) -> str:
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    return "\n".join(lines[1:end]) if end > 0 else ""


def _verify_current_state(file_str: str, kind: str) -> str:
    """判断一条历史 load_error 记录的当前状态（前端据此降级展示）。

    repairable：文件存在且该类异常仍在（可出修复建议）；
    fixed：文件存在但异常已消失（修复过或记录过期）；
    gone：文件不存在（测试残留 / 已删除 / 已改名）。
    """
    path = Path(file_str)
    if not path.is_file():
        return "gone"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return "repairable" if kind == "encoding" else "gone"
    from .frontmatter import parse_frontmatter

    result = parse_frontmatter(raw)
    if kind in ("colon", "unclosed") and not result.meta:
        return "repairable"
    if kind == "colon" and "：" in _frontmatter_block(raw):
        return "repairable"
    return "fixed"


def _classify(event: str, reason: str) -> str:
    if event == "load_failed" or "UnicodeDecodeError" in reason or "codec" in reason:
        return "encoding"
    if "未闭合" in reason:
        return "unclosed"
    if "中文冒号" in reason:
        return "colon"
    return "unknown"


def _normalize_newlines(text: str) -> str:
    """CRLF/CR 统一为 LF（Windows 手编文件常见 CRLF）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fix_chinese_colons(text: str) -> str:
    """frontmatter 区内的 ：→ : （正文不动）。

    替换后冒号后补一个空格（YAML 规范 key: value），已是冒号+空格的不再重复加。
    """
    text = _normalize_newlines(text)
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if end == -1:
        return text
    fixed = []
    for line in lines[:end]:
        if "：" in line and ":" not in line:
            head, _, value = line.partition("：")
            sep = " " if (value and not value.startswith(" ")) else ""
            fixed.append(f"{head}:{sep}{value}")
        else:
            fixed.append(line)
    return "\n".join(fixed + lines[end:])


def _fix_unclosed_frontmatter(text: str) -> str | None:
    """在第一个非 frontmatter 字段行之前补闭合线。

    启发式：frontmatter 字段行形如 key: value 或 key:；遇到首个不匹配的行，
    在其前插入 ---。找不到这样的行则返回 None（歧义过大）。
    """
    text = _normalize_newlines(text)
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "---":
            return None  # 已闭合，不该走到这
        if stripped and ":" not in stripped:
            return "\n".join(lines[:i] + ["---"] + lines[i:])
    return None
