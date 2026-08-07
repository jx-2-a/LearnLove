"""技能插件基类"""

from abc import ABC, abstractmethod


class SkillBase(ABC):
    """技能插件抽象基类

    子类可以覆盖：
    - get_prompt_modifier: 返回动态 prompt_modifier（可依赖上下文）
    - get_tool_schemas: 返回额外的工具 schema（技能可暴露自己的工具）
    - execute_tool: 执行技能特定的工具调用
    """

    name: str = ""
    description: str = ""
    valve: int = 0

    def get_prompt_modifier(self, context: dict = None) -> str:
        """返回追加到系统提示词的文本。默认返回空。"""
        return ""

    def get_tool_schemas(self) -> list[dict]:
        """返回此技能提供的额外工具 schema（OpenAI 格式）"""
        return []

    def execute_tool(self, tool_name: str, **kwargs) -> dict | None:
        """执行技能特定的工具调用。返回 None 表示未处理。"""
        return None
