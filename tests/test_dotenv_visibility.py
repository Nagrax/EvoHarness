""".env 加载可见性回归（2026-10-29 判读 eval/evolution_experiment.py:43）。

判读结论：原 `except Exception: pass` 把三种情况混在一起——
可选依赖缺失（应静默）、文件不存在（常态，应静默）、
**文件存在但解析失败**（用户配了却没生效，必须可见）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SRC = REPO / "eval" / "evolution_experiment.py"


def test_import_error_is_silent():
    """dotenv 未安装属可选依赖缺失，不得报错也不得打印噪声。"""
    text = SRC.read_text(encoding="utf-8")
    assert "except ImportError:" in text, "必须单独捕获 ImportError"
    idx = text.find("except ImportError:")
    window = text[idx: idx + 200]
    assert "pass" in window, "ImportError 分支应静默"
    assert "print" not in window.split("else:")[0], "不得在依赖缺失时打印"


def test_missing_file_is_silent():
    """文件不存在是常态，不得提示。"""
    text = SRC.read_text(encoding="utf-8")
    assert "_env_file.is_file()" in text, "必须先判断存在性"
    idx = text.find("_env_file.is_file()")
    window = text[idx: idx + 260]
    assert "print" not in window.split("except")[0], "存在性判断分支不得打印"


def test_parse_failure_is_visible():
    """文件存在但解析失败必须可见（否则用户以为配置生效了）。"""
    text = SRC.read_text(encoding="utf-8")
    assert "[evolution] .env load failed" in text
    idx = text.find("[evolution] .env load failed")
    window = text[max(0, idx - 300): idx + 200]
    assert "except Exception as error" in window
    assert "type(error).__name__" in window, "必须带异常类型，便于定位"


def test_load_still_happens_for_real_env():
    """正常路径仍要真正加载（不能因为改错误处理就把加载漏掉）。"""
    text = SRC.read_text(encoding="utf-8")
    assert "load_dotenv(_env_file, override=False)" in text
