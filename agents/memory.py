"""
文件型记忆系统。

核心思路：
1. 每个项目有独立的 memory 目录，目录名由当前工作目录 hash 得到。
2. 每条记忆都是一个 Markdown 文件，文件头部用 YAML frontmatter 保存元信息。
3. MEMORY.md 是自动生成的索引，给 system prompt 快速展示已有记忆。
4. 对话时先轻量扫描记忆文件头，再用 side query 让模型挑出相关记忆。
5. 召回到的记忆会以 <system-reminder> 形式注入当前对话。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import locale
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter, format_frontmatter
from typing import Callable
# side query 是一个异步函数：输入 system prompt 和 user prompt，返回模型文本。
# 这里标成 Any 是为了避免在运行时引入复杂 Awaitable 类型约束。
SideQueryFn = Callable[[str, str], Any]  # actually Awaitable[str]


VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200       # MEMORY.md 注入 system prompt 前最多保留的行数。
MAX_INDEX_BYTES = 25000     # MEMORY.md 注入 system prompt 前最多保留的字节数。


# 记忆扫描期的解析失败（结构化保留；扫描函数写、命令层/前端可读）。
# 为什么不是逐条打印：记忆可能有上百条，逐条提示会变噪声；但完全不报会让
# 坏记忆"静默消失"（2026-10-29 判读 silent-except 时确认这是真问题）。
last_scan_errors: list[dict] = []


def _record_scan_error(path: Path, error: BaseException) -> None:
    last_scan_errors.append({
        "file": str(path),
        "name": path.name,
        "error": f"{type(error).__name__}: {error}"[:200],
    })


def _flush_scan_error_summary(stage: str) -> None:
    """把本轮扫描失败汇总成一行提示（有失败才打印）。"""
    if not last_scan_errors:
        return
    names = ", ".join(item["name"] for item in last_scan_errors[:5])
    more = "" if len(last_scan_errors) <= 5 else f" 等 {len(last_scan_errors)} 个"
    print(f"[memory] {stage}: {len(last_scan_errors)} 个文件解析失败（{names}{more}），"
          "这些记忆不会进入索引与召回")


class MemoryEntry:
    """完整 memory 条目，用于 /memory 列表和 CRUD 操作。"""

    __slots__ = ("name", "description", "type", "filename", "content")

    def __init__(self, name: str, description: str, type: str, filename: str, content: str):
        self.name = name
        self.description = description
        self.type = type
        self.filename = filename
        self.content = content




def _project_hash() -> str:
    """用当前工作目录生成稳定 hash，让不同项目的记忆互相隔离。"""
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    """返回当前项目的 memory 目录，不存在时自动创建。"""
    d = Path.home() / ".evoharness" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_index_path() -> Path:
    """MEMORY.md 是当前项目 memory 文件的索引文件。"""
    return get_memory_dir() / "MEMORY.md"




def _slugify(text: str) -> str:
    """把记忆名称转成适合文件名的短 slug。

    归一化是有损的：`'foo bar'` 与 `'foo-bar'` 都压成 `'foo_bar'`，纯中文/非 ASCII 名称
    更会被压成空串——仅对空串兜底哈希不够，非空但归一化相同的名称仍会落同一文件互相覆盖。
    因此在可读前缀后统一附加完整原名的短哈希，保证名称到文件名的映射全局唯一且稳定。
    """
    raw = str(text)
    s = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{s[:40]}_{digest}" if s else digest


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate text to max_bytes of UTF-8 without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _encoding_diagnostics_path() -> Path:
    return get_memory_dir() / "encoding_diagnostics.jsonl"


# 诊断日志写不进去时只警告一次，避免每次调用都往 stderr 刷屏。
_ENCODING_LOG_BROKEN_WARNED = False


def record_encoding_event(event: str, path: Path, detail: str) -> None:
    """把编码异常/迁移事件追加到诊断日志（best-effort，永不抛）。

    Windows 默认环境是本地编码（本机 cp936）：读写异常若被静默吞掉，用户既看不到
    失败也拿不回记忆。这里把事件落盘，作为可诊断依据。
    """
    try:
        row = {
            "event": event,
            "time": datetime.now(timezone.utc).isoformat(),
            "path": str(path),
            "detail": detail,
        }
        p = _encoding_diagnostics_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as error:  # noqa: BLE001
        # 诊断日志自身写失败不能静默：这条日志是"编码/加载问题可追溯"的唯一来源，
        # 静默丢弃会让下次排查回到"什么线索都没有"。这里退到 stderr 并标一次性警告，
        # 避免在日志系统坏掉时反而拖垮主流程。
        global _ENCODING_LOG_BROKEN_WARNED
        if not _ENCODING_LOG_BROKEN_WARNED:
            _ENCODING_LOG_BROKEN_WARNED = True
            try:
                sys.stderr.write(f"[memory] encoding event log unwritable: {type(error).__name__}: {error}\n")
            except Exception:  # noqa: BLE001
                # stderr 也不可用（GUI 无控制台）时不再尝试，避免递归故障。
                pass


def _legacy_encodings() -> list[str]:
    """旧编码候选（严格解码用）。去重保序：先本地编码，再 Windows 中文常见编码。"""
    out: list[str] = []
    for enc in (locale.getpreferredencoding(False), "cp936", "gbk"):
        if enc and enc.lower() not in [e.lower() for e in out]:
            out.append(enc)
    return out


def read_text_migrating(path: Path) -> str:
    """按 UTF-8 契约**只读**地读取记忆/持久化文本（旧编码给出诊断，但不改写文件）。

    旧安装可能残留本地编码（cp936 等）写出的记忆文件：按 UTF-8 读取会抛 UnicodeDecodeError，
    被上层 `except Exception` 静默吞掉，表现为「记忆未召回」。策略：先严格 UTF-8；失败则依次
    尝试旧编码候选的**严格**解码，命中即返回正确文本并留诊断事件。

    刻意**不**在读取时自动写回：一个「非法 UTF-8、但能被 cp936 严格解码」的文件，其内容可能
    本意是 UTF-8（混合/残缺编码），自动迁移会把它固化成乱码。迁移是显式的
    (`migrate_legacy_encoding`)，由用户/工具主动调用。
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raw = path.read_bytes()
        for enc in _legacy_encodings():
            try:
                text = raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
            record_encoding_event(
                "legacy_encoding_read", path,
                f"decoded as {enc} (not UTF-8): {exc}; migrate explicitly via migrate_legacy_encoding()",
            )
            return text
        enc = locale.getpreferredencoding(False) or "utf-8"
        text = raw.decode(enc, errors="replace")
        record_encoding_event(
            "legacy_encoding_read_lossy", path,
            f"no strict legacy decoder succeeded; returned lossy text as {enc} (not migrated)",
        )
        return text


def read_text_migrating_limited(path: Path, max_bytes: int = 65536) -> str:
    """有界读取：只取文件头部 max_bytes 字节，用于解析 frontmatter。

    记忆候选扫描只需要 frontmatter，旧实现却把整份正文读进内存再切前 30 行：
    实测 2 个候选文件就读了 1,560,133 字节（100% 全量）。文件多且大时这是无界开销。
    读取仍走同一套 UTF-8 → 旧编码 → 有损回退契约，编码兼容性不变。
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size <= max_bytes:
        return read_text_migrating(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes)
    except OSError:
        return ""
    # 头部截断可能切断多字节字符：用 errors="replace" 只影响被切断的尾部若干字节，
    # 而 frontmatter 位于文件最前，不受影响。
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        for enc in _legacy_encodings():
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")


def migrate_legacy_encoding(path: Path) -> bool:
    """显式迁移：把非 UTF-8 的旧文件按旧编码严格解码后改写为 UTF-8（幂等）。

    返回是否发生了迁移。仅当严格解码成功才写回；任何候选都失败时返回 False 并留诊断，
    绝不写入有损文本。
    """
    try:
        path.read_text(encoding="utf-8")
        return False  # 已是 UTF-8
    except UnicodeDecodeError:
        pass
    raw = path.read_bytes()
    for enc in _legacy_encodings():
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as write_exc:
            record_encoding_event(
                "legacy_encoding_migrate_failed", path, f"{type(write_exc).__name__}: {write_exc}"
            )
            return False
        record_encoding_event("legacy_encoding_migrated", path, f"rewritten as UTF-8 from {enc}")
        return True
    record_encoding_event("legacy_encoding_migrate_skipped", path, "no strict legacy decoder succeeded")
    return False




def list_memories() -> list[MemoryEntry]:
    """读取当前项目所有 memory 文件，并按修改时间倒序返回。"""
    d = get_memory_dir()
    entries: list[MemoryEntry] = []
    for f in sorted(d.glob("*.md")):
        # MEMORY.md 是索引，不是一条真实记忆。
        if f.name == "MEMORY.md":
            continue
        try:
            result = parse_frontmatter(read_text_migrating(f))
            meta = result.meta
            # 没有 name/type 的文件不算合法 memory。
            if not meta.get("name") or not meta.get("type"):
                continue
            # type 不合法时降级为 project，避免坏文件中断列表。
            t = meta["type"] if meta["type"] in VALID_TYPES else "project"
            entries.append(MemoryEntry(
                name=meta["name"],
                description=meta.get("description", ""),
                type=t,
                filename=f.name,
                content=result.body,
            ))
        except Exception as error:  # noqa: BLE001
            _record_scan_error(f, error)
    _flush_scan_error_summary("index")
    # 最近修改的记忆排在前面，方便 /memory 展示。
    entries.sort(key=lambda e: (d / e.filename).stat().st_mtime, reverse=True)
    return entries


def save_memory(name: str, description: str, type: str, content: str) -> str:
    """保存一条 memory，并刷新 MEMORY.md 索引。"""
    d = get_memory_dir()
    # Sanitize type: a value with path separators (e.g. "../") would escape the memory dir.
    safe_type = type if type in VALID_TYPES else "project"
    filename = f"{safe_type}_{_slugify(name)}.md"
    text = format_frontmatter({"name": name, "description": description, "type": safe_type}, content)
    (d / filename).write_text(text, encoding="utf-8")
    _update_memory_index()
    return filename


def delete_memory(filename: str) -> bool:
    """按文件名删除 memory，删除成功后刷新索引。"""
    filepath = get_memory_dir() / filename
    if not filepath.exists():
        return False
    filepath.unlink()
    _update_memory_index()
    return True




def _update_memory_index() -> None:
    """根据当前 memory 文件重新生成 MEMORY.md。"""
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}) — {m.description}")
    _get_index_path().write_text("\n".join(lines), encoding="utf-8")


def load_memory_index() -> str:
    """读取 MEMORY.md，并在注入 system prompt 前做长度保护。"""
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = read_text_migrating(index_path)
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many memory entries ...]"
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = _truncate_utf8(content, MAX_INDEX_BYTES) + "\n\n[... truncated, index too large ...]"
    return content


# ─── Memory Header (lightweight scan) ──────────────────────

class MemoryHeader:
    """轻量 memory 摘要，只包含召回筛选需要的元信息。"""

    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type", "subject")

    def __init__(self, filename: str, file_path: str, mtime_ms: float,
                 description: str | None, type: str | None,
                 subject: str | None = None):
        self.filename = filename
        self.file_path = file_path
        self.mtime_ms = mtime_ms
        self.description = description
        self.type = type
        # L1 冲突消解主键：同 subject 的旧版本会被标 superseded（生命周期移植）
        self.subject = subject


MAX_MEMORY_FILES = 200                    # 参与召回筛选的最多 memory 文件数。
MAX_MEMORY_BYTES_PER_FILE = 4096          # 单个 memory 注入前的最大字节数。
MAX_SESSION_MEMORY_BYTES = 60 * 1024      # 单个会话最多注入的 memory 总量。
# 单次召回注入上限。LoCoMo 消融显示 5 条上限会损失召回（judge -7.3pp），
# 记忆密集场景可通过环境变量调高，如 EVOHARNESS_MAX_MEMORY_RECALL=15。
# 非法值（如 'abc'）回退默认，避免 import agents.memory 直接抛 ValueError。
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


MAX_MEMORY_RECALL = max(1, _env_int("EVOHARNESS_MAX_MEMORY_RECALL", 5))

# L3 过期降权开关（memory_lifecycle 移植）：默认开启；=0 回退旧行为（对照实验用）。
# 三档语义：off（旧行为）/ soft（标注 stale+superseded，默认）/ filter（硬过滤被取代候选）
def _lifecycle_mode() -> str:
    raw = str(os.environ.get("EVOHARNESS_MEMORY_LIFECYCLE", "soft")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return "off"
    if raw in {"filter", "hard"}:
        return "filter"
    return "soft"


def _lifecycle_enabled() -> bool:
    return _lifecycle_mode() != "off"


def _superseded_map(headers: list["MemoryHeader"]) -> dict[str, str]:
    """同 subject 里最新的为有效版本；返回 {旧文件名: 新文件名}。"""
    newest: dict[str, MemoryHeader] = {}
    for h in headers:
        key = h.subject or ""
        if not key:
            continue
        if key not in newest or h.mtime_ms > newest[key].mtime_ms:
            newest[key] = h
    out: dict[str, str] = {}
    for h in headers:
        key = h.subject or ""
        if key and newest.get(key) is not h:
            out[h.filename] = newest[key].filename
    return out


def _drop_superseded(headers: list["MemoryHeader"]) -> list["MemoryHeader"]:
    """硬过滤：剔除被取代的旧版本（若剔除后为空则保留原集，避免召回全灭）。"""
    superseded = _superseded_map(headers)
    if not superseded:
        return headers
    kept = [h for h in headers if h.filename not in superseded]
    return kept or headers


# 陈旧阈值：超过这个天数在 manifest 里标 [stale Nd]，让选择器能降权。
# 逐次读 env（与 _fold_keep_recent_turns 同一约定），便于对照实验按轮切换。
def _stale_days() -> int:
    try:
        return max(1, int(str(os.environ.get("EVOHARNESS_MEMORY_STALE_DAYS", "30")).strip()))
    except (TypeError, ValueError):
        return 30

# 候选扫描只读头部：frontmatter 在文件最前，无需整读正文。
HEADER_READ_BYTES = max(4096, _env_int("EVOHARNESS_MEMORY_HEADER_BYTES", 65536))

# 触发召回的门槛：中文一行没有空格，只按 \s 判断会让整段中文请求永不召回。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
# 低价值短命令：不消耗 side query（如 ls / pwd / git status 这类一眼可判的指令）。
_LOW_VALUE_COMMANDS = {
    "ls", "dir", "pwd", "cd", "cat", "type", "head", "tail", "echo", "clear", "cls",
    "git status", "git diff", "git log", "whoami", "date", "time", "help", "exit", "quit",
}
MIN_RECALL_QUERY_CHARS = 4


def should_prefetch_memory(query: str) -> tuple[bool, str]:
    """判断这条输入是否值得做记忆召回，并返回原因（便于日志与验收）。

    规则：
    - 空白折叠后为空 → no-query
    - 命中低价值短命令 → low-value-command
    - 含 CJK 且长度 >= MIN_RECALL_QUERY_CHARS → 触发（中文整句通常无空格）
    - 含空白且去空白后长度 >= MIN_RECALL_QUERY_CHARS → 触发（英文多词）
    - 其余 → too-short
    """
    text = " ".join(str(query or "").split())
    if not text:
        return False, "no-query"
    lowered = text.lower().strip()
    if lowered in _LOW_VALUE_COMMANDS:
        return False, "low-value-command"
    compact = re.sub(r"\s+", "", text)
    if _CJK_RE.search(text):
        if len(compact) >= MIN_RECALL_QUERY_CHARS:
            return True, "cjk"
        return False, "too-short"
    if re.search(r"\s", text) and len(compact) >= MIN_RECALL_QUERY_CHARS:
        return True, "multiword"
    return False, "too-short"


def _header_subject(meta: dict, stem: str) -> str:
    """L1 冲突消解主键：frontmatter 的 subject；缺省用文件名规范化（兼容旧记忆）。"""
    raw = str(meta.get("subject") or "").strip()
    if raw:
        return raw.lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", stem.lower()).strip("-")


def scan_memory_headers() -> list[MemoryHeader]:
    """快速扫描 memory 文件头，只读文件头部，用于低成本召回筛选。

    旧实现先整读正文再切前 30 行：候选多且大时读取量无界（实测 100% 全量）。
    现在按 HEADER_READ_BYTES 有界读取，frontmatter 仍在文件最前，语义不变。
    """
    d = get_memory_dir()
    headers: list[MemoryHeader] = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            raw = read_text_migrating_limited(f, HEADER_READ_BYTES)
            # 只解析前 30 行，通常 frontmatter 足够在文件开头完成。
            first30 = "\n".join(raw.split("\n")[:30])
            result = parse_frontmatter(first30)
            meta = result.meta
            t = meta.get("type")
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f),
                mtime_ms=stat.st_mtime * 1000,
                description=meta.get("description"),
                type=t if t in VALID_TYPES else None,
                subject=_header_subject(meta, f.stem),
            ))
        except Exception as error:  # noqa: BLE001
            _record_scan_error(f, error)
    _flush_scan_error_summary("headers")
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """把 memory 摘要列表格式化成给 side query 阅读的 manifest。

    L3 过期降权（memory_lifecycle 移植）：陈旧记忆在 manifest 里显式标注
    `[stale Nd]`，让选择器能看到"这条多久没更新"——这是真正的行为改变：
    旧实现只给时间戳，陈旧记忆与新鲜记忆在候选阶段完全同权。
    开关 EVOHARNESS_MEMORY_LIFECYCLE=0 回退（对照实验用）。
    """
    lifecycle = _lifecycle_enabled()
    stale_days = _stale_days()
    now_ms = time.time() * 1000
    # L1 硬标注：同 subject 里最新的为有效版本，其余标 [superseded by X]。
    # 软性 stale 标注经 A/B 实测无稳定效果（repeats=3 时污染反号），
    # 所以升级为"显式告诉模型这条已被谁取代"——语义判断仍交给模型，但不再靠猜。
    superseded_by = _superseded_map(headers) if lifecycle else {}

    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        marks = ""
        replaced_by = superseded_by.get(h.filename)
        if replaced_by:
            marks += f" [superseded by {replaced_by}]"
        elif lifecycle:
            age_days = max(0, int((now_ms - h.mtime_ms) / 86_400_000))
            if age_days >= stale_days:
                marks += f" [stale {age_days}d]"
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}){marks}: {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts}){marks}")
    return "\n".join(lines)


# ─── Memory Age / Freshness ────────────────────────────────

def memory_age(mtime_ms: float) -> str:
    """把修改时间转换成适合展示的相对时间。"""
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_warning(mtime_ms: float) -> str:
    """旧记忆可能过期，注入时提醒模型先核对当前代码。"""
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (f"This memory is {days} days old. Memories are point-in-time observations, "
            "not live state — claims about code behavior may be outdated. "
            "Verify against current code before asserting as fact.")



SELECT_MEMORIES_PROMPT = """You are selecting memories that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array of filenames for the memories that will clearly be useful (up to {max_select}). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful, do not include it.
- If no memories would clearly be useful, return an empty array."""


def _normalize_selector_ref(value: object) -> str:
    """把选择器返回值/文件名规范化为仅含字母数字与 CJK 的串，用于宽松匹配。

    选择器常照抄 manifest 整行（"name.md (timestamp)"）或包一层代码围栏，
    精确匹配会静默丢失这部分召回（LoCoMo 评测中损失过全部检索结果）。
    """
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value).lower())


class RelevantMemory:
    """被召回并准备注入对话的完整 memory。"""

    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header

    @property
    def size(self) -> int:
        """当前 memory 内容占用的字节数，供 Agent 统计本会话注入预算。"""
        return len(self.content.encode())


async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    """
    从 memory 目录中选择和当前 query 最相关的记忆。

    流程：
    1. 扫描 memory 头信息，避免一开始就读取所有正文。
    2. 排除本 session 已经注入过的 memory，避免重复污染上下文。
    3. 把候选摘要交给 side query，让模型选择最多 5 个文件名。
    4. 读取被选中的 memory 正文，截断过大的文件。
    5. 包装成 RelevantMemory，交给后续注入逻辑。
    """

    # 扫描所有 memory 文件头信息。
    headers = scan_memory_headers()

    if not headers:
        return []

    # 排除已经展示过的 memory。
    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    # L1 第三臂：硬过滤（EVOHARNESS_MEMORY_LIFECYCLE=filter）——把被取代的旧版本
    # 直接从候选池剔除，而不是只加标注。与 soft（标注）/off（无）构成三臂对照。
    if _lifecycle_mode() == "filter":
        candidates = _drop_superseded(candidates)

    # manifest 是给 side query 看的候选摘要列表。
    manifest = format_memory_manifest(candidates)

    # 调用 side_query，让模型根据文件名和描述挑选相关 memory。
    try:
        text = await side_query(
            SELECT_MEMORIES_PROMPT.format(max_select=MAX_MEMORY_RECALL),
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )

        # side query 可能返回解释文本，这里只提取其中的 JSON 对象。
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []

        # 解析 JSON，拿到被选中的 memory 文件名。
        parsed = json.loads(match.group(0))
        # 规范化后做双向包含匹配：兼容选择器返回整行 manifest、带围栏、
        # 或带路径前缀等各种回指格式，避免静默丢召回。
        selected_norms = [
            norm
            for norm in (
                _normalize_selector_ref(item)
                for item in parsed.get("selected_memories", [])
            )
            if norm
        ]
        selected = [
            h
            for h in candidates
            if (key := _normalize_selector_ref(h.filename))
            and any(key in norm or norm in key for norm in selected_norms)
        ]
        # L3 过期降权（memory_lifecycle 移植）：候选超过上限时按"新鲜度"排序再截断，
        # 而不是静默砍掉后面的——两周前的记忆和昨天的记忆不应该同权竞争。
        # 开关 EVOHARNESS_MEMORY_LIFECYCLE=0 可回退旧行为（对照实验用）。
        if len(selected) > MAX_MEMORY_RECALL and _lifecycle_enabled():
            selected = sorted(
                selected, key=lambda h: h.mtime_ms, reverse=True)

        selected = selected[:MAX_MEMORY_RECALL]

        result: list[RelevantMemory] = []
        for h in selected:
            # 读取每个选中的 memory 文件内容。
            content = read_text_migrating(Path(h.file_path))
            # 如果文件太大，就截断，避免单条记忆占用过多上下文。
            if len(content.encode()) > MAX_MEMORY_BYTES_PER_FILE:
                content = _truncate_utf8(content, MAX_MEMORY_BYTES_PER_FILE) + "\n\n[... truncated, memory file too large ...]"

            # 根据 memory 修改时间生成提示头；旧记忆会附带 freshness warning。
            freshness = memory_freshness_warning(h.mtime_ms)
            header_text = (
                f"{freshness}\n\nMemory: {h.file_path}:" if freshness
                else f"Memory (saved {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            # 返回 RelevantMemory 列表，后续会被格式化成 <system-reminder>。
            result.append(RelevantMemory(
                path=h.file_path, content=content,
                mtime_ms=h.mtime_ms, header=header_text,
            ))
        return result
    except Exception as e:
        # 召回失败不应该影响主对话；取消类错误直接静默。
        if "cancel" in str(e).lower():
            return []
        print(f"[memory] semantic recall failed: {e}")
        return []


class MemoryPrefetch:
    """封装 memory 召回异步任务，供 Agent 主循环轮询。"""

    def __init__(self, task: asyncio.Task):
        self.task = task
        # consumed 表示结果是否已经注入过，避免同一个任务结果重复使用。
        self.consumed = False

    @property
    def settled(self) -> bool:
        """任务是否已经完成。"""
        return self.task.done()


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
) -> MemoryPrefetch | None:
    """
    在主模型回复前，提前异步启动 memory 召回。

    返回值不是 memory 内容，而是 MemoryPrefetch 句柄。
    Agent 主循环后续会检查任务是否完成，完成后再把 memory 注入当前消息。
    """

    # 只有值得召回的输入才触发，避免每个短命令都消耗一次 side query。
    # 中文整句没有空格，旧实现的 `re.search(r"\s", query)` 会让它们永不召回。
    should, reason = should_prefetch_memory(query)
    if not should:
        return None

    # 当前 session 的 memory 使用量不能超过预算。
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    # memory 目录里必须真的有 memory 文件。
    d = get_memory_dir()
    has_memories = any(f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir())
    if not has_memories:
        return None

    # 条件通过后创建异步任务，让召回和主模型请求并行推进。
    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """把召回的 memory 包成 system-reminder，便于注入到用户消息。"""
    parts = []
    for m in memories:
        parts.append(f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>")
    return "\n\n".join(parts)


def build_memory_prompt_section() -> str:
    """
    生成注入 system prompt 的 Memory System 说明。

    这段说明告诉模型：
    - memory 文件存放在哪里；
    - 有哪些 memory 类型；
    - 如何通过 write_file 保存 memory；
    - 哪些内容不应该保存；
    - 当前 MEMORY.md 索引里有哪些记忆。
    """
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Save Memories
Use the write_file tool to create a memory file with YAML frontmatter:

```markdown
---
name: memory name
description: one-line description
type: user|feedback|project|reference
---
Memory content here.
```

Save to: `{memory_dir}/`
Filename format: `{{type}}_{{slugified_name}}.md`

The MEMORY.md index is auto-updated when you write to the memory directory — do NOT update it manually.

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Anything already in CLAUDE.md
- Ephemeral task details

## When to Recall
When the user asks you to remember or recall, or when prior context seems relevant.
{chr(10) + "## Current Memory Index" + chr(10) + index if index else chr(10) + "(No memories saved yet.)"}"""
