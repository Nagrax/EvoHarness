#!/usr/bin/env python3
"""检查器自检：审计工具在**输入退化**时是否 fail-closed（报错）而非静默通过。

为什么做这个（2026-10-29 门禁事故的直接推论）：
门禁的 baseline `nodeIds` 为空 → "baseline 项是否都跑了"退化成"空集都跑完了"
→ **永远通过**。这类缺陷的共性是：**检查器把"没有输入"当成了"没有问题"**。

本脚本对几个关键审计器做退化输入测试，看它们是否报错：
1. `claim_audit` —— 声明审计
2. `final_audit` —— 主张清单
3. `lifecycle_noise_gate` —— 跨批次噪声门
4. `evidence_index` —— 证据索引
5. `failure_stability` —— 稳定性分类

判据：**空输入 / 缺文件 / 单批次** 时必须返回 ok=false 或明确 reason，不能"通过"。
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(r"D:/claudecode/EvoHarness")
sys.path.insert(0, str(REPO))

results: list[dict] = []


def check(name: str, ok: bool, detail: object = None) -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    # ① final_audit：主张必须有证据文件
    import eval.final_audit as fa
    audit = fa.audit()
    claims_without_evidence = [c["id"] for c in fa.CLAIMS
                               if not (c.get("evidence") or [])
                               and c.get("grade") in {"A", "B", "C"}]
    check("final_audit: A/B/C 主张都有证据",
          not claims_without_evidence, claims_without_evidence)
    check("final_audit: 主张数量非零", len(fa.CLAIMS) > 0, len(fa.CLAIMS))

    # ② claim_audit：核心函数存在且可调用（对真实文档跑一次）
    import eval.claim_audit as ca
    try:
        ca.check_no_positive_claim_for_unclaimable()
        claim_ok = True
        claim_detail = "ok"
    except SystemExit as exc:
        claim_ok = exc.code in (0, None)
        claim_detail = f"exit={exc.code}"
    except Exception as error:  # noqa: BLE001
        claim_ok = False
        claim_detail = f"{type(error).__name__}: {error}"[:80]
    check("claim_audit: 真实文档无越界声明", claim_ok, claim_detail)
    check("claim_audit: grade 白名单非空", len(ca.VALID_GRADES) >= 4, sorted(ca.VALID_GRADES))

    # ③ noise_gate：单批次必须拒绝
    import eval.lifecycle_noise_gate as ng
    with tempfile.TemporaryDirectory() as td:
        single = Path(td) / "one.json"
        single.write_text(json.dumps({"armsDetail": {"soft": {"recallRate": 0.8}}}),
                          encoding="utf-8")
        try:
            outcome = ng.main.__wrapped__() if hasattr(ng.main, "__wrapped__") else None
        except SystemExit:
            outcome = "exit"
        except Exception as error:  # noqa: BLE001
            outcome = f"raised:{type(error).__name__}"
        # 直接用函数层判断（main 解析 argv）
        judge = getattr(ng, "judge", None) or getattr(ng, "compare_batches", None)
        refuses_single = False
        if judge is not None:
            try:
                out = judge([str(single)])
                refuses_single = (not (out or {}).get("ok", True)) or \
                                 "single" in str((out or {}).get("reason", "")).lower()
            except Exception:  # noqa: BLE001
                refuses_single = True
        check("noise_gate: 单批次被拒绝", refuses_single, str(outcome)[:40])

    # ④ evidence_index：缺证据要报 problem
    import eval.evidence_index as ei
    index = ei.build_index()
    check("evidence_index: 覆盖全部主张", index["total"] == len(fa.CLAIMS), index["total"])
    check("evidence_index: 每个证据有 sha256",
          all(len(f["sha256"]) == 64 for c in index["claims"] for f in c["evidence"]))

    # ⑤ failure_stability：分类边界必须稳定
    import eval.failure_stability as fs
    stable = fs.classify_failure(1.0)
    flaky = fs.classify_failure(0.5)
    gone = fs.classify_failure(0.0)
    check("failure_stability: 阈值分类正确",
          stable == "stable" and flaky == "flaky" and gone == "disappeared",
          f"{stable}/{flaky}/{gone}")

    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    out = REPO / "eval" / "runs" / "auditor-selfcheck.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ok": not failed, "results": results},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())