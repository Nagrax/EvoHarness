"""把本地仓库打包成可直接交接的 zip。

设计原则
--------
1. **只打 git 已跟踪的文件**。这是最强的排除保证：`.env`、`data/agent.db`、
   `data/.token_key`、`.venv`、`__pycache__` 全部从未被 git 追踪，
   因此自然不进包。不依赖手写排除规则（手写规则一定会漏）。
2. **包含 `.git`**。接手方需要提交历史来理解"为什么这么改"，
   且 `git log` 是 20 个提交的唯一完整记录。已验证 git 历史中无真实密钥。
3. **附带敏感文件清单**，明确告知接手方哪些文件需要自己填、不在包里。
4. **自校验**：打包后重新打开 zip，断言危险文件确实不在其中。

用法：
    python eval/package_for_handoff.py
    python eval/package_for_handoff.py --with-db     # 可选：带真实数据库
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

# 这些**绝不能**出现在交接包里（有的已 gitignore，这里再兜一层）
FORBIDDEN_PATTERNS = [
    ".env",
    ".token_key",
    "agent.db",
    "agent.db-wal",
    "agent.db-shm",
    "agent.db.bak",
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    "node_modules/",
]

# 必须出现在包内的配置模板（否则接手方没有参考）
REQUIRED_TEMPLATES = [
    ".env.example",
    "feishu-daily-agent/.env.example",
]


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    return result.stdout


def git_blob(rel: str) -> bytes | None:
    """取文件在 HEAD 中的内容（而不是工作区）。

    为什么重要（2026-11-13 实测）：直接打包工作区文件时，
    若工作区有未提交的本地改动（如台账、评测产物），接手方解压后
    `git status` 会显示脏工作区，看起来像"包有问题"或"有人改了一半"。
    用 HEAD 版本 + 带 .git，解压后就是一个干净的仓库快照。
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", f"HEAD:{rel}"],
        cwd=str(ROOT), capture_output=True, timeout=180,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def tracked_files() -> list[str]:
    return [line for line in git("ls-files").splitlines() if line.strip()]


def is_forbidden(rel: str) -> bool:
    """判断该文件是否禁止入包。

    ⚠️ 这里有个曾经踩过的坑（2026-11-13）：早期实现用
    `name.startswith(pattern)`，于是 `.env.example` 被 `.env` 前缀误杀，
    结果交接包里一个配置模板都没有，接手方不知道要填什么变量。

    正确做法：`.env*` 只拦**真实凭据文件**，模板必须放行。
    """
    name = Path(rel).name

    # 模板白名单：必须在通用规则之前判断
    if name in {".env.example", ".env.sample", ".env.template"}:
        return False

    for pattern in FORBIDDEN_PATTERNS:
        if pattern.endswith("/"):
            if f"/{pattern}" in f"/{rel}":
                return True
        elif name == pattern or name.startswith(pattern):
            return True
    return False


def build(out: Path, *, with_git: bool, with_db: bool) -> tuple[int, int]:
    files = tracked_files()
    skipped: list[str] = []

    stamp = datetime.now().strftime("%Y-%m-%d")
    commit = git("rev-parse", "--short", "HEAD").strip()

    readme = f"""# EvoHarness 交接包

打包时间：{datetime.now().isoformat(sep=' ', timespec='seconds')}
仓库提交：{commit}（分支 master）
基准上游：Nagraax/EvoHarness @ aa0e781（2026-11-09）

## 先读

1. `README.md` 顶部 banner
2. **`docs/AGENT-HANDOFF.md`** —— 接手导航（必读）
3. `docs/与原版的差异.md` —— 和上游 GitHub 版的逐项差异
4. `docs/数字分身-交接文档.md` —— 核心模型与剩余工作

## 本包**不含**的文件（需要自己准备）

| 文件 | 说明 |
|---|---|
| `.env` | 根目录通用配置。复制 `.env.example` 后填 |
| `feishu-daily-agent/.env` | 飞书凭证。复制 `.env.example` 后填 |
| `feishu-daily-agent/data/agent.db` | 真实运行数据库（含业务数据，不随包发） |
| `feishu-daily-agent/data/.token_key` | token 加密密钥（本机专用，换机器要重新生成） |
| `.venv/` | 虚拟环境。自己 `python -m venv .venv` |

配置项含义见 `.env.example` 与 `本地部署指南.md`。

## 起来跑

```bash
python -m venv .venv
.venv\\Scripts\\pip install -r requirements.txt
.venv\\Scripts\\pip install -r feishu-daily-agent\\requirements.txt

cd feishu-daily-agent
cp .env.example .env        # 然后填凭证
..\\.venv\\Scripts\\python.exe -m pytest tests -q     # 预期 246 passed
..\\.venv\\Scripts\\python.exe main.py init-db
```

## 包里有什么

- 完整 git 历史（{len(git('rev-list', '--count', 'HEAD').split()) and git('rev-list', '--count', 'HEAD').strip()} 个提交，含 20 个本地提交）
- 全部源码、测试、文档
- `docs/AGENT-HANDOFF.md` 等 6 份交接文档

## 已知问题（不是包坏了）

- 根级 `tests/` 有 15 failed，**是上游遗留**（对比基准见差异文档 §12）
  - 另有 2 个收集错误：`eval.roi` 模块在上游就不存在
- 飞书子系统 `246 passed` 全绿
"""

    path_docs = ROOT / "_HANDOFF-README.md"
    path_docs.write_text(readme, encoding="utf-8")
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(path_docs, "HANDOFF-README.md")
            for rel in files:
                if is_forbidden(rel):
                    skipped.append(rel)
                    continue
                # 用 HEAD 内容而非工作区：保证解压后 git status 干净
                blob = git_blob(rel)
                if blob is not None:
                    zf.writestr(rel, blob)
                else:
                    src = ROOT / rel
                    if src.is_file():
                        zf.write(src, rel)
            if with_git:
                for item in (ROOT / ".git").rglob("*"):
                    if item.is_file():
                        zf.write(item, item.relative_to(ROOT).as_posix())
            if with_db:
                db = ROOT / "feishu-daily-agent" / "data" / "agent.db"
                if db.is_file():
                    zf.write(db, "feishu-daily-agent/data/agent.db")
    finally:
        path_docs.unlink(missing_ok=True)
    return len(files), len(skipped)


def verify(out: Path) -> list[str]:
    """重新打开 zip，断言危险文件不在其中。"""
    bad: list[str] = []
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    for name in names:
        base = Path(name).name
        # 模板不算危险
        if base in {".env.example", ".env.sample", ".env.template"}:
            continue
        if base == ".env" or base == ".token_key" or base.startswith("agent.db"):
            bad.append(name)
        if "/.venv/" in f"/{name}" or "/__pycache__/" in f"/{name}":
            bad.append(name)
    return bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="输出路径")
    parser.add_argument("--no-git", action="store_true", help="不包含 .git 历史")
    parser.add_argument("--with-db", action="store_true", help="包含真实数据库")
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d")
    out = Path(args.out) if args.out else (
        ROOT.parent / f"EvoHarness-handoff-{stamp}.zip"
    )

    total, skipped = build(out, with_git=not args.no_git, with_db=args.with_db)
    bad = verify(out)

    # 断言模板必须入包：这是 2026-11-13 实际踩过的坑
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    missing_templates = [
        item for item in REQUIRED_TEMPLATES if item not in names
    ]

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"输出: {out}")
    print(f"大小: {size_mb:.2f} MB")
    print(f"追踪文件: {total}，安全跳过: {skipped}")
    if missing_templates:
        print(f"\n❌ 缺少配置模板: {missing_templates}")
        print("   接手方将没有任何配置参考")
        return 1
    if bad:
        print(f"\n❌ 危险：包内含 {len(bad)} 个敏感文件")
        for item in bad[:10]:
            print("   ", item)
        return 1
    print("✅ 自校验通过：无敏感文件，且配置模板齐全")

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
