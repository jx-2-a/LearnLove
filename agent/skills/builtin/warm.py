"""温暖关心风格 — 内置技能"""

from agent.skills.base import SkillBase


class WarmCaringSkill(SkillBase):
    name = "warm_caring"
    description = "温暖关心的沟通风格"
    valve = 0

    # prompt_modifier 已在 skills.yaml 中定义，此处无需覆盖
    # 如需动态 modifier 可覆盖 get_prompt_modifier()
