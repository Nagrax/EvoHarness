#!/usr/bin/env python3
"""`eval/feishu_signal.py --check` 的无参数包装，供 loop_source_health 调用。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import feishu_signal


if __name__ == "__main__":
    raise SystemExit(feishu_signal.main(["--check"]))
