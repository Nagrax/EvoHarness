"""M4 评测工具包：experience-standard/v1 IR 的 golden 集、幻觉探针与 before/after 对比。

本包是纯新增只读工具：不改动 agents/ 任何既有文件，模块级不导入子模块，
保证 `import eval.experience_eval.hallucination_probe` 不会连带触发
agents.experience_standard（M1）的导入——所有 agents.* 依赖一律惰性导入。
"""

__all__ = ["compare_ir", "hallucination_probe"]

__version__ = "0.1.0"
