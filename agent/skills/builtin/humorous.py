"""幽默风趣风格 — 内置技能"""

from agent.skills.base import SkillBase


class HumorousSkill(SkillBase):
    name = "humorous"
    description = "幽默风趣的沟通风格"
    valve = 0
