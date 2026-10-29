"""前端按钮真实点测（浏览器 E2E）：**条件定义**——未开启时连测试都不注册。

为什么用条件定义而不是 skip：
门禁规则禁止新增 skip（防"绿了但没跑"）。skipif 会留下一个 skip 记录并被门禁拦下；
条件定义在未开启时根本没有测试函数，语义更准确：没开启 = 没这项测试。

开启方式：
    $env:EVOHARNESS_UI_E2E = "1"
    python -m pytest tests/test_ui_buttons_live.py -q
可选：EVOHARNESS_UI_CHROME 指定浏览器；EVOHARNESS_UI_PORT 指定端口。

产物：eval/runs/ui/<tag>.json（逐控件结果 + live/conditional/dead 汇总）
实测基线（2026-10-29）：21 个控件 20 live / 1 conditional / 0 dead。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "eval" / "ui" / "buttons.mjs"
OUT_DIR = REPO / "eval" / "runs" / "ui"

_E2E_ON = os.environ.get("EVOHARNESS_UI_E2E", "") == "1"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chrome_path() -> str:
    env = os.environ.get("EVOHARNESS_UI_CHROME")
    if env and Path(env).is_file():
        return env
    for candidate in (
        r"C:/Users/lenovo/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe",
        r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return ""


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """起一个隔离 HOME 的评测服务（不碰真实技能库/记忆）。"""
    home = tmp_path_factory.mktemp("evo-ui-home")
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONUTF8": "1", "PORT": str(port),
           "EVOHARNESS_HOST": "127.0.0.1", "EVO_RELOAD": "0"}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    handle = (OUT_DIR / "live-server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=handle, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.time() + 40
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                time.sleep(1.0)
        assert ready, "评测服务未在 40s 内就绪"
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()


if _E2E_ON:
    def test_all_buttons_are_live_or_explained_conditional(live_server):
        """每个控件必须满足其一：可点测有效 / 明确声明条件渲染并写明触发条件。"""
        if shutil.which("node") is None:
            pytest.fail("node 不可用，但已显式开启 E2E")
        chrome = _chrome_path()
        if not chrome:
            pytest.fail("已开启 E2E 但未找到 Chromium/Edge")

        env = {**os.environ, "EVO_CHROME": chrome, "PYTHONUTF8": "1"}
        proc = subprocess.run(
            ["node", str(SCRIPT), "--base", f"http://127.0.0.1:{live_server}",
             "--out", str(OUT_DIR), "--tag", "ui-pytest"],
            cwd=str(REPO), env=env, capture_output=True, timeout=900, check=False)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")

        report_path = OUT_DIR / "ui-pytest.json"
        assert report_path.is_file(), (
            f"未生成报告；stdout:\n{stdout[-1200:]}\nstderr:\n{stderr[-1200:]}")
        report = json.loads(report_path.read_text(encoding="utf-8"))

        controls = report.get("controls") or []
        assert controls, "报告里没有控件结果"
        dead = [c for c in controls
                if c.get("dead") and not c.get("optional") and not c.get("conditional")]
        conditional = [c for c in controls if c.get("conditional")]
        live = [c for c in controls if not c.get("dead")]

        assert not dead, "存在真正不可用的控件：" + json.dumps(
            [{"name": c.get("name"), "error": c.get("error")} for c in dead],
            ensure_ascii=False)
        for item in conditional:
            assert item.get("conditional"), (
                f"{item.get('name')} 标了 conditional 但没写触发条件")
        assert len(live) >= 20, f"可用控件应 >= 20，实际 {len(live)}"

# ── 修复入口 7 步 e2e（条件定义，同上层）─────────────────────

REPAIR_SCRIPT = REPO / "eval" / "ui" / "repair_entry.mjs"
EVOLUTION_LOG = REPO / ".evoharness" / "skill-evolution" / "skill_load_errors.jsonl"
CLEANUP_SOURCE = "ui-repair-pytest"


def _cleanup_repo_evolution_records() -> int:
    """删除本测试写进仓库的 load_error 记录（按 source 精确匹配）。"""
    if not EVOLUTION_LOG.is_file():
        return 0
    kept, removed = [], 0
    for line in EVOLUTION_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if str(row.get("source")) == CLEANUP_SOURCE:
            removed += 1
        else:
            kept.append(line)
    if removed:
        EVOLUTION_LOG.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


@pytest.fixture()
def repairable_server(tmp_path):
    """起一个含"可修复加载错误"的隔离服务（HOME 隔离，EVOLUTION_DIR 需事后清理）。"""
    skill_md = tmp_path / ".evoharness" / "skills" / "broken-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    # GBK 编码：UTF-8 读取抛 UnicodeDecodeError → kind=encoding → repairable
    skill_md.write_bytes(("---\nname: broken-skill\ndescription: 编码错误技能\n\n正文\n")
                         .encode("gbk", errors="ignore"))

    EVOLUTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "load_failed",
        "file": str(skill_md),
        "reason": "UnicodeDecodeError: 文件不是 UTF-8 编码",
        "source": CLEANUP_SOURCE,
    }
    with EVOLUTION_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    port = _free_port()
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "PYTHONUTF8": "1", "PORT": str(port),
           "EVOHARNESS_HOST": "127.0.0.1", "EVO_RELOAD": "0"}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    handle = (OUT_DIR / "repair-server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=handle, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.time() + 40
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                time.sleep(1.0)
        # 确认真的造出了 repairable（否则后续 e2e 无意义）
        state = ""
        if ready:
            import urllib.request
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/skills", timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                states = [e.get("state") for e in (payload.get("load_errors") or [])]
                state = "repairable" if "repairable" in states else ",".join(states)
            except Exception as error:  # noqa: BLE001
                state = f"probe-failed:{type(error).__name__}"
        yield port, ready, state
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()
        _cleanup_repo_evolution_records()


if _E2E_ON:
    def test_repair_entry_full_flow(repairable_server):
        """修复入口：出现 → 点击 → 弹层 → diff/应用/说明 → 可关闭（7 步全过）。"""
        port, ready, state = repairable_server
        assert ready, "修复入口测试服务未就绪"
        assert state == "repairable", f"未造出 repairable 错误（实际 {state}）"
        chrome = _chrome_path()
        if not chrome:
            pytest.fail("已开启 E2E 但未找到 Chromium/Edge")

        env = {**os.environ, "EVO_CHROME": chrome, "EVO_UI_PORT": str(port),
               "PYTHONUTF8": "1"}
        proc = subprocess.run(["node", str(REPAIR_SCRIPT)], cwd=str(REPO), env=env,
                              capture_output=True, timeout=600, check=False)
        stdout = proc.stdout.decode("utf-8", "replace")
        failed = [line for line in stdout.splitlines() if line.startswith("FAIL")]
        passed = [line for line in stdout.splitlines() if line.startswith("PASS")]
        assert proc.returncode == 0 and not failed, (
            f"修复入口 e2e 有失败步骤：{failed}\nstdout:\n{stdout[-1500:]}")
        assert len(passed) >= 7, f"应通过 >= 7 步，实际 {len(passed)}：{passed}"

