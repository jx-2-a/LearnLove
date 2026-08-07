"""
技能管理器 — YAML 驱动的插件注册表 + 热重载

参考 ALb/Agent/ToolManager.py 的架构。
技能按类别组织：communication_style / situation / coach / custom

技能 = prompt_modifier 文本块 → 激活时注入系统提示词
代码支持的技能可额外暴露工具 schema
"""

import os
import yaml
from typing import Optional


class SkillDef:
    """技能插件定义"""
    __slots__ = ("name", "category", "description", "prompt_modifier",
                 "parameters", "valve", "module_path")

    def __init__(self, name: str, category: str, description: str = "",
                 prompt_modifier: str = "", parameters: dict = None,
                 valve: int = 0, module_path: str = None):
        self.name = name
        self.category = category
        self.description = description
        self.prompt_modifier = prompt_modifier
        self.parameters = parameters or {}
        self.valve = valve
        self.module_path = module_path


class SkillManager:
    """技能插件注册表（单例）"""

    def __init__(self):
        self._skills: dict[str, SkillDef] = {}  # name → SkillDef
        self._yaml_path: str = ""
        self._active: list[str] = []
        self._instances: dict[str, object] = {}  # name → SkillBase instance (for code-backed)

    # ===== 加载 =====

    def load(self, yaml_path: str, active_skills: list[str] = None):
        """加载 skills.yaml"""
        self._yaml_path = yaml_path
        self._active = active_skills or []
        if not os.path.exists(yaml_path):
            return

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for category, defs in data.items():
            if not defs or not isinstance(defs, list):
                continue
            for d in defs:
                name = d.get("name", "")
                if not name:
                    continue
                sd = SkillDef(
                    name=name,
                    category=category,
                    description=d.get("description", ""),
                    prompt_modifier=d.get("prompt_modifier", ""),
                    parameters=d.get("parameters", {}),
                    valve=d.get("valve", 0),
                    module_path=d.get("module", None),
                )
                self._skills[name] = sd

    def reload(self):
        """热重载 skills.yaml"""
        if self._yaml_path:
            self._skills.clear()
            self._instances.clear()
            self.load(self._yaml_path, self._active)

    # ===== 查询 =====

    def get(self, name: str) -> Optional[SkillDef]:
        return self._skills.get(name)

    def list_all(self) -> list[dict]:
        """列出所有技能"""
        return [
            {
                "name": sd.name,
                "category": sd.category,
                "description": sd.description,
                "valve": sd.valve,
                "active": sd.name in self._active,
                "has_module": bool(sd.module_path),
            }
            for sd in self._skills.values()
        ]

    def list_by_category(self, category: str) -> list[SkillDef]:
        return [sd for sd in self._skills.values() if sd.category == category]

    # ===== 激活/停用 =====

    def set_active(self, names: list[str]):
        """设置活跃技能列表"""
        self._active = [n for n in names if n in self._skills]

    def activate(self, name: str) -> bool:
        """激活一个技能"""
        if name not in self._skills:
            return False
        if name not in self._active:
            self._active.append(name)
        return True

    def deactivate(self, name: str) -> bool:
        """停用一个技能"""
        if name in self._active:
            self._active.remove(name)
            return True
        return False

    def get_active(self) -> list[str]:
        return list(self._active)

    # ===== 提示词组装 =====

    def get_prompt_modifiers(self, context: dict = None) -> str:
        """拼接所有活跃技能的 prompt_modifier"""
        parts = []
        for name in self._active:
            sd = self._skills.get(name)
            if not sd or not sd.prompt_modifier:
                continue
            # 如果有代码模块，尝试获取动态 modifier
            instance = self._get_instance(name)
            if instance and hasattr(instance, "get_prompt_modifier"):
                modifier = instance.get_prompt_modifier(context or {})
                if modifier:
                    parts.append(modifier)
            else:
                parts.append(sd.prompt_modifier)
        return "\n\n".join(parts)

    # ===== 工具 schema（代码支持的技能） =====

    def get_schemas(self) -> list[dict]:
        """获取代码支持技能暴露的额外工具 schema"""
        schemas = []
        for name in self._active:
            instance = self._get_instance(name)
            if instance and hasattr(instance, "get_tool_schemas"):
                schemas.extend(instance.get_tool_schemas())
        return schemas

    def dispatch_skill_tool(self, tool_name: str, **kwargs) -> dict:
        """分发到代码支持技能的工具"""
        for name in self._active:
            instance = self._get_instance(name)
            if instance and hasattr(instance, "execute_tool"):
                try:
                    result = instance.execute_tool(tool_name, **kwargs)
                    if result is not None:
                        return result
                except Exception as e:
                    return {"ok": False, "error": str(e)}
        return None  # 未处理

    # ===== 内部 =====

    def _get_instance(self, name: str):
        """获取代码支持技能的实例（延迟加载）"""
        if name in self._instances:
            return self._instances[name]

        sd = self._skills.get(name)
        if not sd or not sd.module_path:
            return None

        try:
            import importlib
            module = importlib.import_module(sd.module_path)
            # 查找 SkillBase 子类
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and
                    attr.__module__ == module.__name__ and
                    hasattr(attr, "name")):
                    instance = attr()
                    self._instances[name] = instance
                    return instance
        except Exception:
            pass
        return None

    # ===== 管理工具（LLM 可调用） =====

    def list_skills(self) -> dict:
        """list_skills 工具：列出所有技能"""
        return {"ok": True, "data": {"skills": self.list_all()}}

    def get_skill(self, name: str) -> dict:
        """get_skill 工具：查看单个技能详情"""
        sd = self._skills.get(name)
        if not sd:
            return {"ok": False, "error": f"技能不存在: {name}"}
        return {"ok": True, "data": {
            "name": sd.name,
            "category": sd.category,
            "description": sd.description,
            "prompt_modifier": sd.prompt_modifier[:300],
            "valve": sd.valve,
            "active": sd.name in self._active,
        }}

    def activate_skill(self, name: str) -> dict:
        """activate_skill 工具：激活技能"""
        if self.activate(name):
            return {"ok": True, "data": {"activated": name, "active": self._active}}
        return {"ok": False, "error": f"技能不存在: {name}"}

    def deactivate_skill(self, name: str) -> dict:
        """deactivate_skill 工具：停用技能"""
        if self.deactivate(name):
            return {"ok": True, "data": {"deactivated": name, "active": self._active}}
        return {"ok": False, "error": f"技能未激活: {name}"}

    def reload_skills(self) -> dict:
        """reload_skills 工具：热重载"""
        self.reload()
        return {"ok": True, "data": {"skills": len(self._skills), "active": self._active}}


# 全局单例
skillmgr = SkillManager()
