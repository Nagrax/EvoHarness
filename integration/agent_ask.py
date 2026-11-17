"""EvoHarness Agent 一次性问答：经本机 Web 服务 /api/chat（SSE）。

需要：server 已启动（python -m server.app）且 .env 配好 APIKEY/API/MODEL。
未配置时返回明确错误，不假装成功。
"""
from __future__ import annotations

import json
import uuid
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration.config import server_url


def ask(question: str, *, timeout: float = 180.0, permission_mode: str = "default") -> dict:
    """向 EvoHarness Agent 提一个问题，收集完整回答。

    permission_mode 用 'default'：涉及代码修改等工具调用需要确认时会暂停，
    一次性问答场景拿不到交互确认，因此纯问答（不派工具活）最合适；
    要派实现类任务请用 integration/task_intake.py 入台账走闭环。
    """
    import httpx

    try:
        base = server_url()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    payload = {
        "session_id": f"feishu-{uuid.uuid4().hex[:8]}",
        "message": question,
        "permission_mode": permission_mode,
    }
    try:
        chunks: list[str] = []
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{base}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")[:200]
                    return {"ok": False, "error": f"HTTP {resp.status_code}: {body}"}
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        continue
                    etype = event.get("type")
                    if etype == "text":
                        chunks.append(event.get("text") or "")
                    elif etype == "error":
                        return {"ok": False, "error": event.get("message") or "agent error"}
                    elif etype == "end":
                        save_err = event.get("saveError")
                        if save_err:
                            chunks.append(f"\n⚠️ 会话保存失败: {save_err}")
        answer = "".join(chunks).strip()
        if not answer:
            return {"ok": False, "error": "empty answer (check .env APIKEY/MODEL)"}
        return {"ok": True, "answer": answer}
    except Exception as exc:  # noqa: BLE001 — 网络/服务不可用要显式可见
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    q = " ".join(sys.argv[1:]) or "你好，请一句话介绍你自己"
    result = ask(q)
    print(json.dumps(result, ensure_ascii=False, indent=2))
