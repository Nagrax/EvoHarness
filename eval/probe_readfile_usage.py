#!/usr/bin/env python3
"""诊断：agent 在长文档任务里是否真的调用 read_file 并装入内容。

> **状态（2026-10-29）：未跑通，勿据此下结论。**
> 阻塞点：探针独立运行时 `_resolve_api_config` 取不到 key（worker 里能取到，
> 差异在调用上下文），报 "Could not resolve authentication method"。
> 已尝试 chat / run_once 两条路径、Agent 构造对齐 worker，均未解决。
> 下次接手请先解决凭据注入（最直接：从这里显式读 `.env` 的 APIKEY 传给 Agent）。
> 该脚本**不进入门禁**，也不作为任何结论的证据。


为什么必须先验证这个：W15 两批实测 folds=0 且 token 每轮约 27K——
**如果 agent 根本没用 read_file**，那么上下文从未装入文档，折叠当然不会触发；
此时调阈值、换文档大小都是白费力气。

输出三项事实（不猜）：
1. 本次运行实际调用了哪些工具（从 messages 里的 tool_use 块提取）
2. 是否有 read_file，读到的内容长度
3. 最终 prediction 是否含校验令牌
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
sys.path.insert(0, str(REPO))

DOC = REPO / "eval" / "runs" / "context" / "longdoc" / "longdoc.md"


async def main() -> int:
    from agents.agent import Agent

    model = os.environ.get("MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")
    agent = Agent(model=model, permission_mode="acceptEdits",
                confirm_fn=lambda *_a, **_k: True)

    prompt = (
        "你现在必须先用 read_file 工具读取这个文件的完整内容：\n"
        f"{DOC}\n"
        "读完后，在附录里找一个形如 ZK-XXXXX 的校验令牌，只输出该令牌。\n"
        "不要只说明意图，必须实际调用工具。"
    )
    text = ""
    try:
        await agent.chat(prompt)  # chat 是 coroutine，内部跑完整轮次
        # 最终回答取历史里最后一条 assistant 文本
        for key in ("_anthropic_messages", "_openai_messages"):
            for msg in reversed(getattr(agent, key, []) or []):
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(
                        str(b.get("text") or "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
                if text:
                    break
            if text:
                break
    except Exception as error:  # noqa: BLE001
        print(f"chat error: {type(error).__name__}: {error}")

    # 从 agent 的消息历史里提取工具调用
    tool_calls: list[dict] = []
    for key in ("_anthropic_messages", "_openai_messages"):
        for msg in getattr(agent, key, []) or []:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls.append({
                            "name": block.get("name"),
                            "bytes": len(json.dumps(block.get("input") or {}, ensure_ascii=False)),
                        })
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        body = block.get("content")
                        size = len(body) if isinstance(body, str) else len(
                            json.dumps(body, ensure_ascii=False))
                        tool_calls.append({"name": "<tool_result>", "resultChars": size})

    names = [c.get("name") for c in tool_calls]
    result_sizes = [c.get("resultChars") for c in tool_calls if c.get("resultChars")]
    report = {
        "ok": True,
        "toolCallNames": names,
        "readFileCalled": any(n in {"read_file", "Read"} for n in names),
        "maxToolResultChars": max(result_sizes) if result_sizes else 0,
        "folds": int(getattr(agent, "_fold_count", 0)),
        "inputTokens": int(getattr(agent, "total_input_tokens", 0)),
        "containsToken": "ZK-" in text,
        "textHead": text[:200],
        "docChars": DOC.stat().st_size if DOC.is_file() else 0,
    }
    out = REPO / "eval" / "runs" / "context" / "longdoc" / "probe-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
