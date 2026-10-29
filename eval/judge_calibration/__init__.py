"""M3 judge 可信度校准（无人工近似）：纯 stdlib、纯函数、零模型调用。

导出：
- cohen_kappa          成对判定的 Cohen's kappa
- swap_consistency     原序/交换序两次判定的一致率与翻转率
- length_controlled_diff  长度分层校正后的 before/after 通过率与差值
"""

from .calib import cohen_kappa, length_controlled_diff, swap_consistency

__all__ = ["cohen_kappa", "swap_consistency", "length_controlled_diff"]

__version__ = "0.1.0"
