"""EvoHarness 飞书机器人 · 本地面板（零依赖，只用标准库）。

解决什么问题
------------
之前这套东西没有入口：要记住三个 .bat，还不知道服务到底活着没有。
面板只回答五个问题，并提供两个动作：

1. 服务在不在跑？（进程 + 长连接 + 最近一次轮询）
2. 今天有没有日报？发出去了吗？
3. 有多少待批草稿、多少条经验？
4. 健康水位怎么样？（心跳/积压/回执未知/事件延迟，读不到显示"未知"）
5. 出事了怎么重启 / 怎么停？

刻意不做的事：不实现"改配置""删数据""批准草稿"——那些在飞书卡片上做，
面板只做观与启停，避免变成第二个需要维护的业务后台。

访问：http://127.0.0.1:8790

安全边界：面板只绑定 127.0.0.1（本机回环），**不得原样暴露公网**——
它有启停动作、会显示部署路径与日志尾部；要远程使用请走带认证的跳板或
反向代理（部署口径见 docs/digital-employee/OPERATIONS.md）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(r"D:\EvoHarness")
AGENT = REPO / "feishu-daily-agent"
DB = AGENT / "data" / "agent.db"
LOG = AGENT / "logs" / "service.out"
TASK_NAME = "EvoHarnessFeishuAgent"
PORT = 8790

# ── 健康水位（DE-23）：与 app/domain/health.py 同一套维度口径 ──
# 面板是零依赖独立进程，不能 import app：这里用只读 SQL 取同一批事实，
# 阈值与 health.py 保持一致；恢复动作文案与 health.py 的 RECOVERY_ACTIONS 对齐。
HEARTBEAT_STALE_SECONDS = 300
DAILY_STALE_SECONDS = 26 * 3600
EVENT_LATENCY_ALERT_SECONDS = 600
INBOX_REPLAY_WINDOW_SECONDS = 21600

HEALTH_ACTIONS = {
    "workerHeartbeat": "启动服务：ops/start.ps1（Windows）或 ops/start.sh（Linux）",
    "dailyHeartbeat": "手动补跑日报（python main.py daily），并检查调度器是否被停用",
    "oldestBacklog": "检查发送通道与限流；先看积压里最老一条的入队原因",
    "outboxReceiptUnknown": "用网关 message_id 逐条对账回执未知的发送，禁止盲重发",
    "eventLatency": "确认事件订阅/轮询是否中断，恢复后观察延迟回落",
    "inboxUnprocessedRecent": "检查事件处理线程是否异常，必要时按回放窗口重放",
    "lastDailyReport": "检查调度器心跳与日报开关，必要时手动补跑",
}

# ── 工作台概览（DE-28）：与工作台/CLI 共用 evoharness/api.py 契约层 ──
# 面板只做观与启停的纪律不变：这里只调统一契约层读数，不自己数业务状态。
_WORK_API = None


def work_summary() -> dict:
    """工作台健康概览；契约层不可用时如实报原因，不用空数据假装正常。"""
    global _WORK_API
    url = (os.environ.get("EVOHARNESS_DATABASE_URL") or "").strip()
    if not url:
        return {"ok": False, "reason_code": "database_not_configured",
                "next_action": "configure_evo_harness_database_url", "retryable": False,
                "data": {}, "evidence_refs": []}
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if _WORK_API is None:
        try:
            from evoharness.api import open_api
        except Exception as exc:  # noqa: BLE001 — 缺依赖要可见
            return {"ok": False, "reason_code": "runtime_unavailable",
                    "detail": f"{type(exc).__name__}: {exc}"[:200], "retryable": False,
                    "data": {}, "evidence_refs": []}
        try:
            _WORK_API = open_api(url)
        except Exception as exc:  # noqa: BLE001 — 库不可用要可见
            return {"ok": False, "reason_code": "database_unavailable",
                    "detail": f"{type(exc).__name__}: {exc}"[:200], "retryable": False,
                    "data": {}, "evidence_refs": []}
    return _WORK_API.view_health()


# ---------------- 状态采集 ----------------

def _ps(script: str) -> str:
    """跑一段 PowerShell 取文本（只读查询）。

    编码坑（2026-11-11 实测，必须记下）：同一句 `schtasks` 在交互式终端里
    输出英文 "Status:"，在 HTTP 服务进程里却输出中文 GBK "状态:"。
    固定用 utf-8 解码会把中文全变成乱码，随后"按关键字匹配状态"永远失败——
    表现为面板显示"已注册（）"，而命令行手跑一切正常。
    这里先按 UTF-8 严格解，失败再退回 GBK，最后兜底 replace。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
            capture_output=True, timeout=30,
        )
    except Exception:
        return ""
    data = out.stdout or b""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def service_processes() -> list[dict]:
    raw = _ps(
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
        "-and $_.CommandLine -like '*main.py*' } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate)\" }"
    )
    result = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        pid, started = line.split("|", 1)
        result.append({"pid": pid.strip(), "startedAt": started.strip()})
    return result


def db_state() -> dict:
    """只读读取 agent.db；文件不存在或表缺失都如实返回。"""
    state: dict = {"reachable": False}
    if not DB.is_file():
        state["error"] = f"数据库不存在：{DB}"
        return state
    try:
        # 面板与业务服务并发访问同一个 SQLite：用只读 URI，避免锁冲突。
        conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row

        def one(sql: str, default=0):
            try:
                row = conn.execute(sql).fetchone()
                return (row[0] if row and row[0] is not None else default)
            except sqlite3.Error:
                return default

        state.update(
            reachable=True,
            messages=one("select count(*) from messages"),
            tasksOpen=one("select count(*) from tasks where status != 'done'"),
            draftsPending=one("select count(*) from reply_drafts where status = 'pending'"),
            draftsSent=one("select count(*) from reply_drafts where status = 'sent'"),
            experiences=one("select count(*) from experiences"),
            experiencesActive=one(
                "select count(*) from experiences where status = 'approved_auto'"
            ),
            dailySent=one("select count(*) from outbox where kind='daily' and status='sent'"),
            outboxFailed=one("select count(*) from outbox where status='failed'"),
        )
        try:
            row = conn.execute(
                "select logical_date, status from daily_reports order by id desc limit 1"
            ).fetchone()
            if row:
                state["lastReport"] = {"date": row[0], "status": row[1]}
        except sqlite3.Error:
            pass
        try:
            row = conn.execute(
                "select value from system_state where key='heartbeat:poller'"
            ).fetchone()
            if row:
                payload = json.loads(row[0])
                state["lastPoll"] = payload.get("at", "")
                state["lastPollStats"] = payload.get("payload", {}).get("stats", {})
        except (sqlite3.Error, ValueError):
            pass
        conn.close()
    except sqlite3.Error as exc:
        state["error"] = f"读取数据库失败：{exc}"
    return state


def health_dimensions() -> dict:
    """健康水位（feishu agent.db 只读）：读取失败显示 unknown，不是空态。

    维度口径与 feishu-daily-agent/app/domain/health.py 一致：
    心跳年龄 / 积压尾部年龄 / 回执未知数 / 事件延迟 / 回放积压 / 最近日报。
    """
    result: dict = {
        "ok": False,
        "status": "unknown",
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dimensions": {},
        "reasonCode": "",
        "nextAction": "",
    }
    if not DB.is_file():
        result.update(reasonCode="database_missing",
                      nextAction=f"业务库不存在：{DB}；确认部署目录与 DATABASE_URL")
        return result
    try:
        conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        result.update(reasonCode="database_unavailable",
                      nextAction=f"数据库打开失败：{exc}")
        return result
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def dimension(name, fn):
        try:
            result["dimensions"][name] = fn()
        except sqlite3.Error as exc:
            result["dimensions"][name] = {
                "status": "unknown", "error": f"{type(exc).__name__}: {exc}"[:160],
                "action": HEALTH_ACTIONS.get(name, ""),
            }

    def heartbeat_dim(key, stale_seconds):
        row = conn.execute(
            "select value from system_state where key=?", (f"heartbeat:{key}",)
        ).fetchone()
        if row is None:
            return {"status": "degraded", "value": None, "action": HEALTH_ACTIONS.get(
                "workerHeartbeat" if key == "worker" else "dailyHeartbeat", "")}
        payload = json.loads(row[0])
        at = datetime.fromisoformat(payload["at"])
        age = max(0.0, (now - at).total_seconds())
        return {"status": "ok" if age <= stale_seconds else "stale",
                "value": round(age, 1), "unit": "seconds_since_heartbeat", "at": payload["at"]}

    dimension("workerHeartbeat", lambda: heartbeat_dim("worker", HEARTBEAT_STALE_SECONDS))
    dimension("dailyHeartbeat", lambda: heartbeat_dim("daily", DAILY_STALE_SECONDS))

    def backlog_tail():
        row = conn.execute(
            "select min(created_at) from outbox where status in "
            "('queued','retry_wait','delivery_unknown')"
        ).fetchone()
        if row is None or row[0] is None:
            return {"status": "ok", "value": None, "unit": "seconds"}
        created = datetime.fromisoformat(row[0])
        age = max(0.0, (now - created).total_seconds())
        return {"status": "degraded" if age > 900 else "ok",
                "value": round(age, 1), "unit": "seconds"}

    dimension("oldestBacklog", backlog_tail)

    def receipt_unknown():
        row = conn.execute(
            "select count(*) from outbox where status='delivery_unknown'"
        ).fetchone()
        count = int(row[0] or 0)
        return {"status": "degraded" if count else "ok", "value": count, "unit": "messages",
                "action": HEALTH_ACTIONS.get("outboxReceiptUnknown", "") if count else ""}

    dimension("outboxReceiptUnknown", receipt_unknown)

    def event_latency():
        row = conn.execute(
            "select min(received_at) from inbox_events where processed_at is null"
        ).fetchone()
        if row is None or row[0] is None:
            return {"status": "ok", "value": None, "unit": "seconds"}
        age = max(0.0, (now - datetime.fromisoformat(row[0])).total_seconds())
        return {"status": "degraded" if age > EVENT_LATENCY_ALERT_SECONDS else "ok",
                "value": round(age, 1), "unit": "seconds"}

    dimension("eventLatency", event_latency)

    def inbox_unprocessed():
        cutoff = (now - timedelta(seconds=INBOX_REPLAY_WINDOW_SECONDS)).isoformat()
        row = conn.execute(
            "select count(*) from inbox_events where processed_at is null and received_at >= ?",
            (cutoff,),
        ).fetchone()
        count = int(row[0] or 0)
        return {"status": "degraded" if count > 1000 else "ok", "value": count,
                "unit": "events"}

    dimension("inboxUnprocessedRecent", inbox_unprocessed)

    def last_report():
        row = conn.execute(
            "select logical_date, status from daily_reports order by id desc limit 1"
        ).fetchone()
        if row is None:
            return {"status": "idle", "value": None}
        return {"status": "ok", "value": f"{row[0]}（{row[1]}）"}

    dimension("lastDailyReport", last_report)
    conn.close()

    statuses = {d.get("status") for d in result["dimensions"].values()}
    if "unknown" in statuses:
        result["status"] = "unknown"
    elif "degraded" in statuses or "stale" in statuses:
        result["status"] = "degraded"
    else:
        result["status"] = "ok"
    result["ok"] = result["status"] != "unknown"
    return result


def task_state() -> dict:
    # 用 CIM 而不是 schtasks 文本：schtasks 会按控制台代码页输出中英文混排
    # （同一句命令在终端是 "Status:"、在服务里是 "状态:"），解析注定脆。
    # Get-ScheduledTask 返回结构化字段，且我们显式指定输出编码。
    raw = _ps(
        "Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.TaskName)|$($_.State)\" }" % TASK_NAME
    )
    for line in raw.splitlines():
        if "|" in line:
            name, _, state = line.partition("|")
            if name.strip() == TASK_NAME:
                return {"name": TASK_NAME, "registered": True, "status": state.strip()}
    # 失败时退回 schtasks，并把原文一并返回，避免"空状态"这种无声失败。
    fallback = _ps(f"schtasks /query /tn {TASK_NAME} /fo LIST")
    status = ""
    for line in fallback.splitlines():
        if ":" not in line:
            continue
        head, _, tail = line.partition(":")
        if head.strip().lower() in {"status", "状态"}:
            status = tail.strip()
            break
    return {
        "name": TASK_NAME,
        "registered": bool(fallback.strip()),
        "status": status,
        "raw": fallback[:200] if not status else "",
    }


def log_tail(lines: int = 12) -> list[str]:
    if not LOG.is_file():
        return []
    try:
        text = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return text[-lines:]


def runtime_config() -> dict:
    """从 .env 读出与人直接相关的几项（不读数据库、不导入应用模块）。"""
    cfg = {
        "mode": "?",
        "allowAllChats": False,
        "groupMentionOnly": True,
        "autoReply": False,
    }
    env = AGENT / ".env"
    if not env.is_file():
        return cfg
    values: dict[str, str] = {}
    try:
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip()
    except OSError:
        return cfg
    cfg["mode"] = values.get("FEISHU_DEPLOYMENT_MODE", "?")
    cfg["allowAllChats"] = values.get("FEISHU_ALLOW_ALL_CHATS", "0") in {"1", "true", "True"}
    cfg["groupMentionOnly"] = values.get("FEISHU_GROUP_REPLY_ONLY_MENTION", "1") in {
        "1", "true", "True"
    }
    cfg["autoReply"] = values.get("FEISHU_AUTO_REPLY_ENABLED", "0") in {"1", "true", "True"}
    cfg["dailyTime"] = values.get("DAILY_REPORT_TIME", "08:30")
    return cfg


def status() -> dict:
    procs = service_processes()
    running = bool(procs)
    return {
        "now": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "running": running,
        "processes": procs,
        "task": task_state(),
        "config": runtime_config(),
        "db": db_state(),
        "logTail": log_tail(),
    }


# ---------------- 动作 ----------------

def start_service() -> dict:
    if service_processes():
        return {"ok": True, "message": "已在运行，无需重复启动"}
    subprocess.run(
        ["schtasks", "/run", "/tn", TASK_NAME],
        capture_output=True, text=True, timeout=30,
    )
    return {"ok": True, "message": "已通过计划任务启动（约 15 秒后可用）"}


def stop_service() -> dict:
    procs = service_processes()
    if not procs:
        return {"ok": True, "message": "本来就没在运行"}
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
        "-and $_.CommandLine -like '*main.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    _ps(script)
    remaining = service_processes()
    if remaining:
        return {"ok": False, "message": f"仍有 {len(remaining)} 个进程未退出"}
    return {"ok": True, "message": "已停止（计划任务会在下次开机/重试时再拉起）"}


# ---------------- 页面 ----------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>EvoHarness 飞书机器人</title>
<style>
 body{font-family:-apple-system,"Segoe UI",Microsoft YaHei,sans-serif;margin:0;background:#f6f7f9;color:#1f2329}
 .wrap{max-width:900px;margin:0 auto;padding:24px}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#8f959e;font-size:13px;margin-bottom:18px}
 .card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:16px 18px;margin-bottom:14px}
 .row{display:flex;flex-wrap:wrap;gap:26px}
 .item{min-width:104px}
 .k{color:#8f959e;font-size:12px;margin-bottom:4px}
 .v{font-size:22px;font-weight:600}
 .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:13px;font-weight:600}
 .on{background:#e8ffea;color:#00875a}.off{background:#ffece8;color:#cb2634}
 .warn{background:#fff7e8;color:#b25f00}
 button{font-size:14px;padding:8px 16px;border-radius:8px;border:1px solid #d0d3d6;background:#fff;cursor:pointer;margin-right:8px}
 button.primary{background:#1664ff;border-color:#1664ff;color:#fff}
 button:disabled{opacity:.45;cursor:not-allowed}
 pre{background:#1f2329;color:#d0d3d6;padding:12px;border-radius:8px;font-size:12px;overflow:auto;max-height:230px;margin:0}
 .hint{color:#8f959e;font-size:12px;line-height:1.7}
 code{background:#f2f3f5;padding:1px 5px;border-radius:4px}
</style></head><body><div class="wrap">
<h1>EvoHarness 飞书机器人</h1>
<div class="sub" id="sub">读取中…</div>

<div class="card">
  <div class="row" style="align-items:center;justify-content:space-between">
    <div><div class="k">服务状态</div><div id="svc" class="v">…</div></div>
    <div>
      <button class="primary" id="btnStart" onclick="act('start')">启动</button>
      <button id="btnStop" onclick="act('stop')">停止</button>
      <button onclick="load()">刷新</button>
    </div>
  </div>
</div>

<div class="card"><div class="row" id="stats"></div></div>

<div class="card">
  <div class="k" style="margin-bottom:8px">当前规则</div>
  <div id="cfg" class="hint"></div>
</div>

<div class="card">
  <div class="k" style="margin-bottom:8px">最近日志</div>
  <pre id="log">…</pre>
</div>

<div class="card">
  <div class="k" style="margin-bottom:8px">工作台概览</div>
  <pre id="work">…</pre>
</div>

<div class="card">
  <div class="k" style="margin-bottom:8px">健康水位</div>
  <div id="health" class="hint">读取中…</div>
</div>

<div class="card">
  <div class="k" style="margin-bottom:8px">怎么用</div>
  <div class="hint">
    1. 在飞书里私聊 <b>Carlcare数字员工</b>，直接发问题。<br>
    2. 群里要 <b>@它</b> 才回应（不 @ 只静默记录，不回你）。<br>
    3. 你的回复会先变成 <b>草稿</b>，进日报卡片；你点「批准」才会发出去，<br>
    &nbsp;&nbsp;&nbsp;并且这时候才会把它沉淀成一条经验。<br>
    4. 想让某条经验以后自动用：在飞书发 <code>验证 #编号</code>。
  </div>
</div>
</div>
<script>
function cell(k,v){return `<div class="item"><div class="k">${k}</div><div class="v">${v}</div></div>`}
async function act(what){
  const r = await fetch('/api/'+what,{method:'POST'});
  const j = await r.json();
  alert(j.message||'ok');
  setTimeout(load,1200);
}
async function load(){
  try{
    const s = await (await fetch('/api/status')).json();
    document.getElementById('sub').textContent = '更新于 ' + s.now;
    document.getElementById('svc').innerHTML = s.running
      ? '<span class="badge on">运行中</span>'
      : '<span class="badge off">未运行</span>';
    document.getElementById('btnStart').disabled = s.running;
    document.getElementById('btnStop').disabled = !s.running;
    const d = s.db||{};
    document.getElementById('stats').innerHTML =
      cell('消息', d.messages ?? '-') +
      cell('待批草稿', d.draftsPending ?? '-') +
      cell('未完成任务', d.tasksOpen ?? '-') +
      cell('经验总数', d.experiences ?? '-') +
      cell('可用经验', d.experiencesActive ?? '-') +
      cell('日报已发', d.dailySent ?? '-') +
      cell('发送失败', d.outboxFailed ?? '-');
    const c = s.config||{};
    document.getElementById('cfg').innerHTML =
      `模式 <code>${c.mode}</code>　`
      + `全群放行 <code>${c.allowAllChats?'开':'关'}</code>　`
      + `群聊必须@ <code>${c.groupMentionOnly?'是':'否'}</code>　`
      + `自动回复 <code>${c.autoReply?'开':'关'}</code>　`
      + `日报 <code>${c.dailyTime||'08:30'}</code>`
      + (d.lastReport?`<br>最近日报：${d.lastReport.date}（${d.lastReport.status}）`:'')
      + (d.lastPoll?`<br>最近轮询：${d.lastPoll}`:'')
      + (s.task&&s.task.registered?`<br>开机自启：已注册（${s.task.status||'-'}）`:'<br><span style="color:#cb2634">开机自启：未注册</span>');
    document.getElementById('log').textContent = (s.logTail||[]).join('\\n') || '（无日志）';
    try{
      const w = await (await fetch('/api/work')).json();
      const el = document.getElementById('work');
      if(!w.ok){
        el.textContent = '不可用：' + (w.reason_code||'unknown')
          + (w.next_action ? ('（下一步: ' + w.next_action + '）') : '');
      }else{
        const d = w.data||{};
        const runs = (d.runs&&d.runs.by_state)||{};
        const runLine = Object.entries(runs).map(([k,v])=>k+' '+v).join(' · ') || '无 Run';
        const wait = Object.entries(d.waiting||{}).map(([k,v])=>k+' '+v).join(' · ') || '无等待项';
        el.textContent = 'Run：' + runLine + '\\n'
          + '等待：' + wait + '\\n'
          + '回执未知：' + ((d.receipts&&d.receipts.delivery_unknown)||0) + '\\n'
          + '预算限额：' + ((d.budget&&d.budget.limits_configured) ? '已配置' : '未配置')
          + (d.at ? ('\\n更新于 ' + d.at.slice(0,19).replace('T',' ')) : '');
      }
    }catch(e){
      document.getElementById('work').textContent = '读取失败：' + e;
    }
    try{
      const h = await (await fetch('/api/health')).json();
      const el = document.getElementById('health');
      const nameMap = {workerHeartbeat:'Worker 心跳', dailyHeartbeat:'日报心跳',
        oldestBacklog:'积压尾部年龄', outboxReceiptUnknown:'回执未知',
        eventLatency:'事件延迟', inboxUnprocessedRecent:'回放积压',
        lastDailyReport:'最近日报'};
      if(!h.ok && h.status === 'unknown'){
        el.innerHTML = '<span class="badge off">未知</span> 原因：'
          + (h.reasonCode||'read_failed')
          + (h.nextAction ? ('<br>下一步：' + h.nextAction) : '');
      }else{
        const lines = Object.entries(h.dimensions||{}).map(([k,v])=>{
          const cls = v.status==='ok' ? 'on' : (v.status==='idle' ? 'warn' : 'off');
          const label = v.status==='ok' ? '正常'
            : (v.status==='idle' ? '未启用' : (v.status==='unknown' ? '未知' : '异常'));
          const val = (v.value===null||v.value===undefined) ? '' :
            ('：' + v.value + (v.unit ? ' ' + v.unit : ''));
          const err = v.error ? ('（读取失败：' + v.error + '）') : '';
          const act = (v.status!=='ok' && v.status!=='idle' && v.action)
            ? ('<br>&nbsp;&nbsp;恢复动作：' + v.action) : '';
          return nameMap[k] + ' <span class="badge ' + cls + '">' + label + '</span>'
            + val + err + act;
        });
        el.innerHTML = (lines.join('<br>') || '无维度')
          + '<br><span style="color:#8f959e">整体：' + (h.status||'?') + '，更新于 '
          + (h.checkedAt||'') + '</span>';
      }
    }catch(e){
      document.getElementById('health').textContent = '读取失败：' + e;
    }
  }catch(e){
    document.getElementById('sub').textContent = '读取状态失败：'+e;
  }
}
load(); setInterval(load, 10000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — http.server 约定
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            payload = json.dumps(status(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/api/work":
            payload = json.dumps(work_summary(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/api/health":
            payload = json.dumps(health_dimensions(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
        elif self.path == "/api/_taskraw":
            # 仅本机可访问的诊断端点：暴露任务查询的原始返回，便于定位解析问题。
            raw = _ps(f"schtasks /query /tn {TASK_NAME} /fo LIST")
            body = json.dumps(
                {"raw": raw, "len": len(raw), "state": task_state()}, ensure_ascii=False
            ).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/start":
            result = start_service()
        elif self.path == "/api/stop":
            result = stop_service()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *args) -> None:
        pass  # 面板自身不刷屏


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # 只绑回环：面板带启停动作与日志/路径信息，禁止原样暴露公网（见模块 docstring）。
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"EvoHarness 面板已启动：http://127.0.0.1:{PORT}")
    print("关闭此窗口即停止面板（飞书机器人不受影响）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
