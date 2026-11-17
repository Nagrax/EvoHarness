"""Skill 搜索增强包（SKILL-EVOLUTION-SEARCH，DE-29 起逐步落位）。

S1（DE-29）提供两个最小契约模块，均为纯离线、可合成验证：

- ``contracts``：SearchManifest——身份门（只有明确独立执行 SOP 可建立）、
  完整 bundle 冻结（正文+辅助脚本+工具集 → bundle_hash/bundle_id）、
  评测协议指纹（bundle 任何组成变化 → 新协议 → 旧评测不可复用）、
  冻结期源目录只读守卫、与 DE-18 publish/CAS 的组合口径。
- ``modules``：最小 Module 定位——复用 agents.frontmatter 解析（不引入第二
  Skill 格式）；小 Skill 单可写正文 Module；多 Module 按既有业务段落标题
  外置定位（span+锚点 hash）；交叠/不唯一/陈旧 hash/外部编辑拒绝；
  默认不向用户 SKILL.md 添加任何定位标记。

真实模型能力实验（预算/审核真值/隔离执行环境）另行 blockedOn，见任务 DE-29。
"""
