"""把一次 OAuth 结果落库（加密），并验证可取用 token。"""
from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.oauth_server import save_authorization  # noqa: E402

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="显式指定已有 OAuth 结果；新授权建议 oauth_server.py --once --save")
    parser.add_argument("--expected-open-id", default=None)
    args = parser.parse_args(argv)
    data = json.loads(args.input.read_text(encoding="utf-8"))
    result = save_authorization(data, expected_open_id=args.expected_open_id)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
