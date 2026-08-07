"""技能加载器"""

import yaml

from agent.skills.base import SkillBase


def load_skill_from_def(skill_def) -> SkillBase | None:
    """从 YAML 定义中实例化技能（有 module 路径的代码支持技能）"""
    if not skill_def.module_path:
        return None

    try:
        import importlib
        module = importlib.import_module(skill_def.module_path)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and
                issubclass(attr, SkillBase) and
                attr is not SkillBase and
                getattr(attr, "name", None) == skill_def.name):
                return attr()
    except Exception as e:
        print(f"[!] 加载技能 {skill_def.name} 失败: {e}")
    return None


def get_active_modifiers(skillmgr, context: dict = None) -> str:
    """获取所有活跃技能的 prompt_modifier 拼接"""
    return skillmgr.get_prompt_modifiers(context)
