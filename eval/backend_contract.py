#!/usr/bin/env python3
"""EvoHarness 后端契约评测：逐个端点验证真实功能，而不是只看 200。

设计原则：
- 每个端点配「正向 + 至少一个负向」断言，负向必须能被拒绝或明确报错；
- 只断言可机器验证的事实（状态码、字段、副作用、安全拒绝），不断言文案；
- 全部跑在隔离临时 home/cwd 下，不污染真实记忆与 Skill；
- 产出 JSON 报告 + 机器可读的 findings，供迭代闭环消费。

用法：
  python eval/backend_contract.py [--out PATH] [--json]
退出码：0 全部通过；1 有失败用例。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _isolate_env(tmp: Path) -> dict:
    """把 HOME/USERPROFILE 指到临时目录，避免读写真实 ~/.evoharness。"""
    env = dict(os.environ)
    env["HOME"] = str(tmp)
    env["USERPROFILE"] = str(tmp)
    env["PYTHONUTF8"] = "1"
    return env


class Case:
    def __init__(self, name: str, endpoint: str, kind: str, description: str) -> None:
        self.name = name
        self.endpoint = endpoint
        self.kind = kind  # happy | negative | security
        self.description = description


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results: list[dict] = []
    findings: list[dict] = []

    def record(name, endpoint, kind, ok, detail, error=""):
        results.append({
            "case": name, "endpoint": endpoint, "kind": kind,
            "ok": bool(ok), "detail": detail, "error": error,
        })
        if not ok:
            findings.append({
                "id": f"backend:{name}",
                "endpoint": endpoint,
                "kind": kind,
                "detail": detail,
                "error": error[:600],
            })

    with tempfile.TemporaryDirectory(prefix="evo-backend-", ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        # 隔离：进程内改环境变量（server.app 在 import 时读取，所以先设再导入）
        os.environ["HOME"] = str(tmp)
        os.environ["USERPROFILE"] = str(tmp)
        os.chdir(tmp)

        try:
            import importlib

            import server.app as appmod
            importlib.reload(appmod)
            from fastapi.testclient import TestClient
            client = TestClient(appmod.app)
        except Exception as error:  # noqa: BLE001
            print(json.dumps({"ok": False, "fatal": f"{type(error).__name__}: {error}",
                              "trace": traceback.format_exc()[-1500:]}, ensure_ascii=False, indent=2))
            return 1

        # ── ① GET /  静态首页 ─────────────────────────────
        try:
            r = client.get("/")
            ok = r.status_code == 200 and ("<html" in r.text.lower() or "<!doctype" in r.text.lower())
            record("index_serves_html", "GET /", "happy", ok,
                   {"status": r.status_code, "bytes": len(r.text), "hasHtmlTag": "<html" in r.text.lower()})
        except Exception as error:  # noqa: BLE001
            record("index_serves_html", "GET /", "happy", False, {}, str(error))

        # ── ② GET /api/health ─────────────────────────────
        try:
            r = client.get("/api/health")
            body = r.json() if r.status_code == 200 else {}
            required = {"ok", "model", "protocol", "sessions", "server_configured", "server_key_default"}
            ok = r.status_code == 200 and body.get("ok") is True and required.issubset(body)
            record("health_contract", "GET /api/health", "happy", ok,
                   {"status": r.status_code, "missingFields": sorted(required - set(body)), "body": body})
            # 负向：health 不应泄漏 key
            leaked = any("key" in k.lower() and isinstance(v, str) and len(v) > 20 for k, v in body.items())
            record("health_no_key_leak", "GET /api/health", "security", not leaked, {"leaked": leaked})
        except Exception as error:  # noqa: BLE001
            record("health_contract", "GET /api/health", "happy", False, {}, str(error))

        # ── ③ GET /api/history ────────────────────────────
        try:
            r = client.get("/api/history", params={"session_id": "sess-probe"})
            body = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and isinstance(body, (list, dict))
            record("history_empty_ok", "GET /api/history", "happy", ok,
                   {"status": r.status_code, "type": type(body).__name__})
            # 负向：路径穿越必须被拒
            bad = client.get("/api/history", params={"session_id": "../../etc/passwd"})
            record("history_rejects_traversal", "GET /api/history", "security",
                   bad.status_code >= 400, {"status": bad.status_code})
        except Exception as error:  # noqa: BLE001
            record("history_empty_ok", "GET /api/history", "happy", False, {}, str(error))

        # ── ④ GET /api/skills ─────────────────────────────
        try:
            r = client.get("/api/skills")
            body = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and isinstance(body, dict) and ("skills" in body or body.get("mock") is not None)
            record("skills_listing", "GET /api/skills", "happy", ok,
                   {"status": r.status_code, "keys": sorted(body)[:8] if isinstance(body, dict) else None})
        except Exception as error:  # noqa: BLE001
            record("skills_listing", "GET /api/skills", "happy", False, {}, str(error))

        # ── ⑤ GET /api/skills/tiers ───────────────────────
        try:
            r = client.get("/api/skills/tiers")
            body = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and isinstance(body, dict)
            record("skill_tiers_listing", "GET /api/skills/tiers", "happy", ok,
                   {"status": r.status_code, "keys": sorted(body)[:8] if isinstance(body, dict) else None})
        except Exception as error:  # noqa: BLE001
            record("skill_tiers_listing", "GET /api/skills/tiers", "happy", False, {}, str(error))

        # ── ⑤c GET /api/daily/latest 飞书日报只读投影 ─────
        # 两分支：DB 存在 → available:true 且带 report 结构；
        # DB 缺失 → 安静降级 available:false（绝不 5xx，不影响主服务）。
        try:
            r = client.get("/api/daily/latest")
            body = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and isinstance(body.get("available"), bool)
            if body.get("available"):
                rep = body.get("report") or {}
                ok = ok and {"logical_date", "status", "summary", "sections"}.issubset(rep)
                ok = ok and isinstance(rep.get("sections"), dict)
            record("daily_latest_contract", "GET /api/daily/latest", "happy", ok,
                   {"status": r.status_code, "available": body.get("available")})
        except Exception as error:  # noqa: BLE001
            record("daily_latest_contract", "GET /api/daily/latest", "happy", False, {}, str(error))
        try:
            original_db = appmod._FEISHU_DAILY_DB
            appmod._FEISHU_DAILY_DB = original_db.parent / "no-such-agent.db"
            r = client.get("/api/daily/latest")
            body = r.json() if r.status_code == 200 else {}
            ok = (r.status_code == 200 and body.get("available") is False
                  and isinstance(body.get("reason"), str))
            record("daily_latest_missing_db_degrades", "GET /api/daily/latest", "negative", ok,
                   {"status": r.status_code, "reason": body.get("reason")})
        except Exception as error:  # noqa: BLE001
            record("daily_latest_missing_db_degrades", "GET /api/daily/latest", "negative", False, {}, str(error))
        finally:
            appmod._FEISHU_DAILY_DB = original_db

        # ── ⑤d /api/work/* 组织契约：无库时也必须是可读信封（不 5xx）──
        work_gets = [
            ("work_skills_envelope", "/api/work/skills", {}),
            ("work_schema_envelope", "/api/work/schema", {}),
            ("work_view_envelope", "/api/work/view", {"tenant": "local", "owner": "founder"}),
            ("work_item_envelope", "/api/work/item", {"task_id": 1}),
            ("work_evidence_envelope", "/api/work/evidence", {"task_id": 1}),
            ("work_approvals_envelope", "/api/work/approvals", {"org": "org-demo"}),
            ("work_cost_envelope", "/api/work/cost", {"org": "org-demo"}),
            ("work_health_envelope", "/api/work/health", {"org": "org-demo"}),
            ("work_knowledge_envelope", "/api/work/knowledge",
             {"space": "space-support", "subject": "登录问题受理口径"}),
            ("work_org_envelope", "/api/work/org", {"org": "org-demo"}),
        ]
        for name, path, params in work_gets:
            try:
                r = client.get(path, params=params)
                body = r.json() if r.status_code == 200 else {}
                ok = (r.status_code == 200 and isinstance(body, dict) and "ok" in body)
                if body.get("ok") is True:
                    ok = ok and "data" in body
                else:
                    ok = ok and isinstance(body.get("reason_code"), str)
                record(name, f"GET {path}", "negative", ok,
                       {"status": r.status_code, "ok": body.get("ok"),
                        "reason": body.get("reason_code")})
            except Exception as error:  # noqa: BLE001
                record(name, f"GET {path}", "negative", False, {}, str(error))

        try:
            r = client.post("/api/work/op", json={"op": "schema", "params": {}})
            body = r.json() if r.status_code == 200 else {}
            ok = (r.status_code == 200 and isinstance(body, dict)
                  and ("ok" in body)
                  and (body.get("ok") is True or isinstance(body.get("reason_code"), str)))
            record("work_op_envelope", "POST /api/work/op", "negative", ok,
                   {"status": r.status_code, "ok": body.get("ok"),
                    "reason": body.get("reason_code")})
        except Exception as error:  # noqa: BLE001
            record("work_op_envelope", "POST /api/work/op", "negative", False, {}, str(error))

        # ── ⑤e /api/skills/search* 搜索命名空间契约 ──────────
        try:
            r = client.get("/api/skills/search")
            body = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and isinstance(body.get("searches"), list)
            record("skills_search_overview", "GET /api/skills/search", "happy", ok,
                   {"status": r.status_code, "searches": len(body.get("searches") or [])})
            # 负向：只读端点不接受 POST（方法约束是契约的一部分）
            bad = client.post("/api/skills/search")
            record("skills_search_overview_rejects_post", "GET /api/skills/search",
                   "negative", bad.status_code == 405, {"status": bad.status_code})
        except Exception as error:  # noqa: BLE001
            record("skills_search_overview", "GET /api/skills/search", "happy", False, {}, str(error))

        try:
            r = client.get("/api/skills/search/detail", params={"search_id": "no-such-search"})
            body = r.json() if r.status_code in (200, 404) else {}
            ok = (r.status_code == 404 and body.get("ok") is False
                  and body.get("error") == "search_state_not_found")
            record("skills_search_detail_missing", "GET /api/skills/search/detail", "negative", ok,
                   {"status": r.status_code, "error": body.get("error")})
        except Exception as error:  # noqa: BLE001
            record("skills_search_detail_missing", "GET /api/skills/search/detail", "negative",
                   False, {}, str(error))

        try:
            r = client.post("/api/skills/search/action",
                            json={"search_id": "no-such-search", "action": "bogus"})
            body = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and body.get("ok") is False
            record("skills_search_action_rejects_unknown",
                   "POST /api/skills/search/action", "negative", ok,
                   {"status": r.status_code, "ok": body.get("ok")})
        except Exception as error:  # noqa: BLE001
            record("skills_search_action_rejects_unknown",
                   "POST /api/skills/search/action", "negative", False, {}, str(error))

        # ── ⑤b GET /api/skills 与 /api/skills/tiers 不得泄漏敏感信息 ──
        # 这两个端点只有 happy 用例时，"响应结构没变"校验不到"内容泄漏"。
        # 判据：序列化后的响应不含凭据标记、不含盘符绝对路径。
        # 凭据按**形态**匹配，不用裸子串（"task-specific" 里也有 "sk-"）。
        _secret_patterns = (
            r"sk-[A-Za-z0-9_-]{20,}",          # OpenAI 风格
            r"sk-ant-[A-Za-z0-9_-]{10,}",      # Anthropic 风格
            r'"api_key"\s*:\s*"[^"]{8,}"',   # 显式带值的 api_key 字段
            r"anthropic_api_key\s*=\s*\S{8,}",
        )
        _path_patterns = (r"C:\\\\", r"D:\\\\")  # 盘符绝对路径

        try:
            r = client.get("/api/skills")
            import re as _re

            payload = r.text if r.status_code == 200 else ""
            leaked = [pat for pat in _secret_patterns if _re.search(pat, payload)]
            leaked += [pat for pat in _path_patterns if _re.search(pat, payload)]
            record("no_secret_leak_skills", "GET /api/skills", "security", not leaked,
                   {"status": r.status_code, "leaked": leaked[:3], "size": len(payload)})
        except Exception as error:  # noqa: BLE001
            record("no_secret_leak_skills", "GET /api/skills", "security", False, {}, str(error))

        try:
            r = client.get("/api/skills/tiers")
            import re as _re

            payload = r.text if r.status_code == 200 else ""
            leaked = [pat for pat in _secret_patterns if _re.search(pat, payload)]
            leaked += [pat for pat in _path_patterns if _re.search(pat, payload)]
            record("no_secret_leak_tiers", "GET /api/skills/tiers", "security", not leaked,
                   {"status": r.status_code, "leaked": leaked[:3], "size": len(payload)})
        except Exception as error:  # noqa: BLE001
            record("no_secret_leak_tiers", "GET /api/skills/tiers", "security",
                   False, {}, str(error))

        # ── ⑥ POST /api/skills/tiers/action ───────────────
        try:
            r = client.post("/api/skills/tiers/action", json={"skill": "no-such-skill", "action": "promote"})
            ok = r.status_code >= 400 or (r.status_code == 200 and r.json().get("ok") is False)
            record("tier_action_rejects_unknown_skill", "POST /api/skills/tiers/action", "negative", ok,
                   {"status": r.status_code, "body": r.text[:200]})
        except Exception as error:  # noqa: BLE001
            record("tier_action_rejects_unknown_skill", "POST /api/skills/tiers/action",
                   "negative", False, {}, str(error))

        # ── ⑦ POST /api/skills/repair/suggest 越界文件 ────
        try:
            r = client.post("/api/skills/repair/suggest", json={"file": "C:/Windows/System32/drivers/etc/hosts"})
            ok = r.status_code >= 400 or (r.status_code == 200 and r.json().get("ok") is False)
            record("repair_rejects_outside_root", "POST /api/skills/repair/suggest", "security", ok,
                   {"status": r.status_code, "body": r.text[:200]})
        except Exception as error:  # noqa: BLE001
            record("repair_rejects_outside_root", "POST /api/skills/repair/suggest",
                   "security", False, {}, str(error))

        # ── ⑦b POST /api/skills/repair/apply 越界文件必须拒绝 ──
        # 这个端点会真的改文件，越界路径是安全边界；happy path 需要真实可修文件，
        # 契约层只验证"拒绝语义 + 响应可读"（修复成功的正例由 tests/test_skill_repair.py 覆盖）。
        try:
            r = client.post("/api/skills/repair/apply",
                            json={"file": "C:/Windows/System32/drivers/etc/hosts",
                                  "event": "load-failed", "reason": "probe"})
            body = {}
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {}
            ok = (r.status_code >= 400
                  or (r.status_code == 200 and body.get("ok") is False))
            record("repair_apply_rejects_outside_root",
                   "POST /api/skills/repair/apply", "security", ok,
                   {"status": r.status_code, "body": r.text[:200]})
        except Exception as error:  # noqa: BLE001
            record("repair_apply_rejects_outside_root",
                   "POST /api/skills/repair/apply", "security", False, {}, str(error))

        # ── ⑧ GET /api/eval/report, /api/eval/skills ──────
        for ep in ("/api/eval/report", "/api/eval/skills"):
            try:
                r = client.get(ep)
                ok = r.status_code == 200 and isinstance(r.json(), (dict, list))
                record(f"eval_{ep.rsplit('/', 1)[-1]}_readable", f"GET {ep}", "happy", ok,
                       {"status": r.status_code})
            except Exception as error:  # noqa: BLE001
                record(f"eval_{ep.rsplit('/', 1)[-1]}_readable", f"GET {ep}", "happy", False, {}, str(error))

        # ── ⑨ POST /api/stop 未知会话 ─────────────────────
        try:
            r = client.post("/api/stop", json={"session_id": "sess-never-existed"})
            ok = r.status_code in (200, 404) and not (r.status_code == 500)
            record("stop_unknown_session_no_crash", "POST /api/stop", "negative", ok,
                   {"status": r.status_code, "body": r.text[:160]})
            bad = client.post("/api/stop", json={"session_id": "../../evil"})
            record("stop_rejects_traversal", "POST /api/stop", "security", bad.status_code >= 400,
                   {"status": bad.status_code})
        except Exception as error:  # noqa: BLE001
            record("stop_unknown_session_no_crash", "POST /api/stop", "negative", False, {}, str(error))

        # ── ⑩ POST /api/chat 无 key 必须明确拒绝并给 end ──
        try:
            with client.stream("POST", "/api/chat",
                               json={"session_id": "sess-chat-probe", "message": "hello",
                                     "api_key": "", "api_base": "", "model": ""}) as resp:
                text = "".join(chunk for chunk in resp.iter_text())
            has_error = '"type": "error"' in text or '"type":"error"' in text
            has_end = '"type": "end"' in text or '"type":"end"' in text
            record("chat_without_key_errors_cleanly", "POST /api/chat", "negative",
                   has_error and has_end, {"hasError": has_error, "hasEnd": has_end, "bytes": len(text)})
            record("chat_no_key_leak_in_stream", "POST /api/chat", "security",
                   "sk-" not in text and "APIKEY" not in text, {"leaked": "sk-" in text})
            bad = client.post("/api/chat", json={"session_id": "../../x", "message": "hi"})
            record("chat_rejects_traversal", "POST /api/chat", "security", bad.status_code >= 400,
                   {"status": bad.status_code})
        except Exception as error:  # noqa: BLE001
            record("chat_without_key_errors_cleanly", "POST /api/chat", "negative", False, {}, str(error))

        # ── ⑪ 静态资源与 favicon 不应 500 ─────────────────
        for path in ("/docs", "/openapi.json"):
            try:
                r = client.get(path)
                record(f"doc_route_{path.strip('/').replace('/', '_')}_reachable",
                       f"GET {path}", "happy", r.status_code in (200, 404), {"status": r.status_code})
            except Exception as error:  # noqa: BLE001
                record(f"doc_route_{path.strip('/')}", f"GET {path}", "happy", False, {}, str(error))

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    report = {
        "kind": "backend-contract",
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "ok": passed == total and total > 0,
        "results": results,
        "findings": findings,
    }
    out = Path(args.out) if args.out else REPO / "eval" / "runs" / "backend-contract.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"backend-contract: {passed}/{total} passed, {total - passed} failed -> {out}")
        for f in findings:
            print(f"  FAIL {f['id']}: {f['detail']} {f['error'][:120]}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
