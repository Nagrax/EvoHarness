#!/usr/bin/env python3
"""提交前敏感文件审计：绝不把凭据/profile/大产物带进 git。

为什么必须做（2026-10-29 用户授权 git 后的第一件事）：
本仓库里有几类**绝不能进版本库**的东西：
1. `LIVE_BUDGET.json` —— 含 `credentials` / `endpoint` 字段
2. `.env` / `*.key` / `*.pem`
3. 浏览器 profile（cookie / localStorage / 登录态）
4. 大型产物（单文件 > 5MB 的抓取页 / 包）
5. 运行产物目录（eval/runs、.evoharness 的会话数据）

本脚本做三件事：
① 扫描 **未跟踪 + 已修改** 文件里的敏感模式
② 报告**会被提交**的高风险文件
③ 给出 .gitignore 建议（不自动改）
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")

# 敏感文件名/路径模式
SENSITIVE_PATH = [
    r"(^|/)\.env($|\.)",
    r"(^|/)\.envrc$",
    r"\.key$", r"\.pem$", r"\.p12$", r"\.pfx$",
    r"LIVE_BUDGET\.json$",
    r"credentials",
    r"browser-profile",
    r"(^|/)\.ssh/",
    r"id_rsa",
    r"cookies?\.(json|txt)$",
    r"\.evoharness/",
    r"^eval/runs/",
    r"^eval/data/",
    r"(^|/)node_modules/",
    r"__pycache__/",
    r"\.pyc$",
]

# 文件内容里的凭据模式
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "openai-style key"),
    (r"AKIA[0-9A-Z]{16}", "aws access key"),
    (r"ghp_[A-Za-z0-9]{20,}", "github token"),
    (r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", "private key"),
    (r"(?i)api[_-]?key\s*[:=]\s*['\"][^'\"]{12,}", "api_key assignment"),
    (r"(?i)password\s*[:=]\s*['\"][^'\"]{8,}", "password assignment"),
    (r"(?i)bearer\s+[A-Za-z0-9._-]{20,}", "bearer token"),
]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache"}
BIG_FILE = 5 * 1024 * 1024


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True,
                          text=True, timeout=180).stdout


def candidate_files() -> list[Path]:
    """未跟踪 + 已修改 + 已暂存的文件。"""
    out: set[str] = set()
    for line in git("status", "--porcelain").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename
            path = path.split(" -> ")[-1]
        out.add(path.strip('"'))
    return [REPO / p for p in sorted(out)]


def main() -> int:
    files = candidate_files()
    sensitive: list[dict] = []
    secrets: list[dict] = []
    big: list[dict] = []

    for path in files:
        rel = path.relative_to(REPO).as_posix()
        for pattern in SENSITIVE_PATH:
            if re.search(pattern, rel):
                sensitive.append({"path": rel, "rule": pattern})
                break
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > BIG_FILE:
            big.append({"path": rel, "mb": round(size / 1024 / 1024, 1)})
        # 只扫文本，且跳过明显二进制
        if size > 1_000_000 or path.suffix.lower() in {".png", ".jpg", ".zip", ".exe", ".dll"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, text):
                secrets.append({"path": rel, "kind": label})
                break

    report = {
        "ok": not (sensitive or secrets or big),
        "candidateFiles": len(files),
        "sensitivePaths": sensitive[:40],
        "possibleSecrets": secrets[:40],
        "oversize": big[:20],
    }
    out = REPO / "eval" / "runs" / "git-preflight.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"candidate files: {len(files)}")
    print(f"\n敏感路径（必须排除）: {len(sensitive)}")
    for item in sensitive[:12]:
        print(f"  {item['path'][:80]}  <- {item['rule']}")
    print(f"\n疑似凭据（必须检查）: {len(secrets)}")
    for item in secrets[:12]:
        print(f"  {item['path'][:70]}  [{item['kind']}]")
    print(f"\n超大文件: {len(big)}")
    for item in big[:8]:
        print(f"  {item['path'][:70]}  {item['mb']} MB")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())