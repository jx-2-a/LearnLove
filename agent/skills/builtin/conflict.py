"""矛盾化解 — 内置情景技能"""

from agent.skills.base import SkillBase


class ConflictResolutionSkill(SkillBase):
    name = "conflict_resolution"
    description = "处理关系中的分歧或冷战"
    valve = 1
