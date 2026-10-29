"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

from .agent import Agent
from .session import load_stage_segment, load_session, get_latest_session_id
from .stages import load_manifest
from .ui import (
    print_welcome,
    print_user_prompt,
    print_error,
    print_info,
    print_plan_for_approval,
    print_plan_approval_options,
    print_goodbye,
    print_interrupted,
    print_memory_entries,
    print_skill_entries,
    print_warning,
)
from .session_memory import format_stage_handover
from .memory import list_memories
from .skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    execute_skill,
    get_skill_by_name,
    record_feedback,
    skill_stats,
)
from .online_skill_eval import format_online_skill_eval_async


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evoharness",
        description="EvoHarness — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=50, help="Max agentic turns (default 50)")
    parser.add_argument(
        "--skills-tier",
        nargs="+",
        default=None,
        help="Deployment tier ops (no agent, no API key): list | promote <skill> | rollback <skill> | core <skill>",
    )
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _run_skills_tier_command(parts: list[str]) -> None:
    """部署分区人工操作入口（评审第 3 条）：manual_promote / 一键回退 / core 晋级的 CLI 承载。"""
    from . import skill_tiers

    action = str(parts[0] or "").strip().lower()
    if action == "list":
        print(skill_tiers.format_tiers())
        return
    if len(parts) < 2 or not str(parts[1] or "").strip():
        print("usage: --skills-tier list | promote <skill> | rollback <skill> | core <skill>")
        sys.exit(1)
    skill_name = str(parts[1]).strip()
    if action == "promote":
        result = skill_tiers.manual_promote(skill_name)
    elif action == "rollback":
        result = skill_tiers.rollback(skill_name)
    elif action == "core":
        result = skill_tiers.manual_promote_core(skill_name)
    else:
        print(f"unknown --skills-tier action: {action}")
        sys.exit(1)
    if result.get("ok"):
        print(f"{skill_name}: {result.get('from_tier')} -> {result.get('to_tier')} ({result.get('trigger')})")
    else:
        print(f"failed: {result.get('error')}")
        sys.exit(1)


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _load_env_file() -> None:
    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)


def _is_anthropic_compatible_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    path = (parsed.path or "").lower().rstrip("/")
    return path.endswith("/anthropic") or "/anthropic/" in path


def _resolve_api_config(cli_api_base: str | None) -> tuple[str | None, str | None, bool]:
    generic_api_key = _clean_env(os.environ.get("APIKEY")) or _clean_env(os.environ.get("EVOHARNESS_API_KEY"))
    openai_api_key = _clean_env(os.environ.get("OPENAI_API_KEY"))
    anthropic_api_key = _clean_env(os.environ.get("ANTHROPIC_API_KEY"))

    generic_api_base = _clean_env(os.environ.get("API")) or _clean_env(os.environ.get("EVOHARNESS_API_BASE"))
    openai_api_base = _clean_env(os.environ.get("OPENAI_BASE_URL"))
    anthropic_api_base = _clean_env(os.environ.get("ANTHROPIC_BASE_URL"))

    resolved_api_base = _clean_env(cli_api_base) or generic_api_base or openai_api_base or anthropic_api_base

    if resolved_api_base:
        if _is_anthropic_compatible_base_url(resolved_api_base):
            return resolved_api_base, generic_api_key or anthropic_api_key or openai_api_key, False
        return resolved_api_base, generic_api_key or openai_api_key or anthropic_api_key, True

    if anthropic_api_key or anthropic_api_base:
        return anthropic_api_base, generic_api_key or anthropic_api_key or openai_api_key, False

    if openai_api_key or openai_api_base:
        return openai_api_base, generic_api_key or openai_api_key or anthropic_api_key, True

    if generic_api_key:
        return None, generic_api_key, False

    return None, None, False


def _agent_is_processing(agent: Agent) -> bool:
    """Agent 是否正在处理一轮：chat() 运行中 _turn_output_buffer 非 None；
    旧判据误用 _output_buffer（仅 run_once() 置非 None，REPL 走 chat() 时恒为 None）。"""
    return agent._aborted is False and agent._turn_output_buffer is not None


async def run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print_warning("Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if _agent_is_processing(agent):
            # Agent is processing
            agent.abort()
            print_interrupted()
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print_goodbye()
                sys.exit(0)
            print_warning("Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print_goodbye()
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print_goodbye()
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_memory_entries(memories)
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .evoharness/skills/<name>/SKILL.md")
            else:
                print_skill_entries(skills)
            continue
        if inp == "/skill-stats":
            print_info(skill_stats())
            continue
        if inp == "/skill-eval":
            side_query = agent._build_side_query(max_tokens=2400, label="skill_eval")
            print_info(await format_online_skill_eval_async(side_query=side_query))
            continue
        if inp.startswith("/extract_now"):
            hint = inp[len("/extract_now") :].strip()
            result = await agent.extract_now(hint)
            if result.get("ok"):
                print_info("Ran online skill extraction for the current pending window.")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-feedback "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 2)
            if len(parts) < 2:
                print_error("Usage: /skill-feedback <skill-name> <rating> [note]")
                continue
            note = parts[2] if len(parts) > 2 else ""
            record_feedback(parts[0], parts[1], note)
            print_info(f"Recorded feedback for skill: {parts[0]}")
            continue
        if inp.startswith("/skill-evolve "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 1)
            if len(parts) < 2:
                print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
                continue
            result = evolve_skill(parts[0], parts[1], rationale="Manual REPL evolution", target="active")
            if result.get("ok"):
                print_info(f"Evolved skill {result.get('skill')} to version {result.get('version')}")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-create "):
            _, rest = inp.split(" ", 1)
            parts = [part.strip() for part in rest.split("|", 3)]
            if len(parts) < 4 or not all(parts[:4]):
                print_error("Usage: /skill-create <name> | <description> | <when-to-use> | <instructions>")
                continue
            result = create_skill(
                name=parts[0],
                description=parts[1],
                when_to_use=parts[2],
                instructions=parts[3],
                target="project",
                context="inline",
                user_invocable=False,
                evidence="Manual REPL skill creation",
            )
            if result.get("ok"):
                print_info(f"Created skill {result.get('skill')} at {result.get('file')}")
            else:
                print_error(str(result.get("error") or result))
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        await agent.chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        result = execute_skill(skill.name, cmd_args)
                        if not result:
                            print_error(f"Unknown skill: {skill.name}")
                            continue
                        await agent.chat(result["prompt"])
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))
    await agent.drain_background_skill_tasks()


async def run_one_shot(agent: Agent, prompt: str) -> None:
    await agent.chat(prompt)
    await agent.drain_background_skill_tasks()


def _resume_staged_session(agent: Agent, metadata: dict) -> bool:
    """--resume staged 分支（改造二）：恢复到最近阶段边界而非任意中间态。

    评审 #6：不自动恢复权限档——manifest 记录的 advisory_mode 只是咨询信息，
    resume 后有效权限一律维持 Agent 构造档（default），提权需用户显式确认。
    评审 #8：manifest 用全局会话 metadata 的 plan_dir_abs 绝对路径定位，跨 cwd 可恢复；
    目录/manifest 缺失 → 返回 False，回退全局原始存档并显式告警。
    评审 #9：无任何交接记录（如 plan 段内即崩溃）→ 返回 False 回退并提示重新激活。
    """
    plan_dir = Path(str(metadata.get("plan_dir_abs") or ""))
    manifest = load_manifest(plan_dir)
    if manifest is None:
        print_warning(f"Stage manifest missing at {plan_dir} — falling back to raw history restore.")
        return False
    stages_list = list(manifest.get("stages") or [])
    target = None
    skipped = 0
    for record in reversed(stages_list):
        if record.get("handover"):
            target = record
            break
        skipped += 1
    if target is None:
        print_warning(
            "阶段机未形成边界（无任何交接记录，如首段内即崩溃）——回退全局原始存档；"
            "可用 --plan 重新激活阶段机。"
        )
        return False
    if skipped:
        print_warning(f"{skipped} 个续段无交接（如预算强制收段），恢复点回退到最近阶段边界。")
    idx = stages_list.index(target)
    from_stage = str(stages_list[idx - 1].get("name")) if idx > 0 else "start"
    # 存档指针指向上一段（已关闭、已落盘）的段文件；target 自己的文件要等它关闭才写。
    archive_file = str(plan_dir / str(stages_list[idx - 1].get("session_file"))) if idx > 0 else ""
    content = format_stage_handover(
        target["handover"],
        from_stage=from_stage,
        to_stage=str(target.get("name")),
        stage_file=archive_file,
    )
    if agent.use_openai:
        agent.restore_session({"openaiMessages": [
            {"role": "system", "content": agent._system_prompt},
            {"role": "user", "content": content},
        ]})
    else:
        agent.restore_session({"anthropicMessages": [{"role": "user", "content": content}]})
    timeline = " -> ".join(f"{r.get('name')}({r.get('status')})" for r in stages_list)
    print_info(
        f"Resumed at stage boundary '{target.get('name')}' (plan {manifest.get('plan_id')}).\n"
        f"  Stage timeline: {timeline}\n"
        f"  Recorded advisory permission mode: {target.get('advisory_mode') or 'n/a'} — "
        "resume stays in the current mode; re-confirm elevation explicitly."
    )
    return True


def main() -> None:
    """CLI 程序入口：准备运行配置，创建 Agent，并按参数选择一次性执行或交互模式。"""
    # 解析命令行参数，例如 --plan、--resume、--model，以及可选的一次性 prompt。
    args = parse_args()
    _load_env_file()

    if args.help:
        # 自定义帮助文本，展示 EvoHarness 支持的启动参数和 REPL 内置命令。
        print("""
Usage: evoharness [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits; shell commands still require confirmation
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: deepseek-chat, or MODEL env)
  --api-base URL      Override API base URL from CLI or .env
  --resume            Resume the last session
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /memory             List saved memories
  /skills             List available skills
  /skill-stats        Show skill usage and evolution stats
  /skill-eval         Evaluate online skill evolution quality
  /extract_now        Extract the current pending online skill window: /extract_now [hint]
  /skill-feedback     Record feedback: /skill-feedback <skill> <rating> [note]
  /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
  /skill-create       Create a skill: /skill-create <name> | <description> | <when-to-use> | <instructions>
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  evoharness "fix the bug in src/app.ts"
  evoharness --yolo "run all tests and fix failures"
  evoharness --plan "how would you refactor this?"
  evoharness --max-cost 0.50 --max-turns 20 "implement feature X"
  MODEL=deepseek-chat APIKEY=sk-xxx API=https://api.deepseek.com/anthropic evoharness "hello"
  MODEL=gpt-4o OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://aihubmix.com/v1 evoharness "hello"
  evoharness --resume
  evoharness  # starts interactive REPL
""")
        sys.exit(0)

    if args.skills_tier:
        # 部署分区人工操作：不启动 Agent、不需要 API key（纯文件运维）。
        _run_skills_tier_command(args.skills_tier)
        return

    # 将命令行布尔开关统一转换成 Agent 内部使用的权限模式。
    permission_mode = _resolve_permission_mode(args)
    # 模型优先使用命令行参数，其次读取 .env 中的 MODEL，最后回落到默认模型。
    model = args.model or os.environ.get("MODEL") or "deepseek-chat"
    resolved_api_base, resolved_api_key, resolved_use_openai = _resolve_api_config(args.api_base)

    # 没有可用 API key 时无法调用模型，直接提示配置方式并退出。
    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env for generic config,\n"
            "  or use ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    # 创建主 Agent。OpenAI-compatible 和 Anthropic 原生接口使用不同的 base URL 参数名传入。
    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
        # Loop 阶段状态机（改造二评审 #5）：仅 --plan CLI 显式激活；REPL /plan、
        # enter_plan_mode 与子 Agent 不激活。
        loop_stages=args.plan,
    )

    # Resume session
    # --resume 会加载最近一次会话，把历史消息恢复到新建的 Agent 中。
    if args.resume:
        session_id = get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                metadata = session.get("metadata") or {}
                # staged 会话（改造二）：恢复到最近阶段边界；失败/非 staged 回退原始历史。
                restored = False
                if metadata.get("loop_staged") and metadata.get("plan_dir_abs"):
                    restored = _resume_staged_session(agent, metadata)
                if not restored:
                    agent.restore_session({
                        # 上次落盘协议：跨协议恢复时据其把历史迁进当前协议的列表。
                        "protocol": metadata.get("protocol"),
                        "anthropicMessages": session.get("anthropicMessages"),
                        "openaiMessages": session.get("openaiMessages"),
                    })
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    # 如果命令行后面带了普通文本参数，就拼成一次性 prompt；否则进入交互式 REPL。
    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        # One-shot mode
        # 一次性模式：执行完用户 prompt 后进程结束。
        try:
            asyncio.run(run_one_shot(agent, prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Interactive REPL
        # 交互模式：启动循环读取用户输入，直到用户退出。
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
