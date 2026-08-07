"""狗头军师 — 内置教练技能

提供全面的恋爱分析和策略建议。
激活后，Agent 以恋爱顾问视角分析每次对话。
"""

from agent.skills.base import SkillBase


class GoutoujunshiSkill(SkillBase):
    name = "goutoujunshi"
    description = "狗头军师 — 全面的恋爱指导，分析关系信号，设计推进策略"
    valve = 1

    def get_tool_schemas(self) -> list[dict]:
        """暴露关系分析工具"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "analyze_relationship_signal",
                    "description": "分析对方的最新消息中隐含的关系信号。从兴趣度、情绪状态、投入度三个维度评估，给出客观分析",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string", "description": "对方的最新消息文本"},
                            "context_summary": {"type": "string", "description": "最近的对话上下文摘要（可选）"},
                        },
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "suggest_next_move",
                    "description": "根据当前关系阶段和对方状态，建议下一步的推进策略。包括：建议的主动程度、适合的邀约方式、应避免的雷区",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "relationship_stage": {"type": "string", "description": "当前关系阶段，如 '暧昧期', '刚认识', '稳定交往'"},
                            "recent_vibe": {"type": "string", "description": "最近的对话氛围，如 '轻松愉快', '有点冷淡', '对方主动'"},
                        },
                        "required": ["relationship_stage"],
                    },
                },
            },
        ]

    def execute_tool(self, tool_name: str, **kwargs) -> dict | None:
        """执行军师工具"""
        if tool_name == "analyze_relationship_signal":
            return self._analyze_signal(**kwargs)
        elif tool_name == "suggest_next_move":
            return self._suggest_move(**kwargs)
        return None

    def _analyze_signal(self, message: str, context_summary: str = "") -> dict:
        """分析关系信号（标记性实现，实际分析由 LLM 完成）"""
        return {
            "ok": True,
            "data": {
                "message": message,
                "note": "详细的信号分析由 LLM 在回复中提供。此工具用于标注需要分析的场景。",
                "dimensions": ["兴趣度", "情绪状态", "投入度", "可推进信号"],
            },
        }

    def _suggest_move(self, relationship_stage: str, recent_vibe: str = "") -> dict:
        return {
            "ok": True,
            "data": {
                "stage": relationship_stage,
                "vibe": recent_vibe,
                "note": "策略建议由 LLM 根据上下文综合生成。此工具标注需要策略建议的场景。",
            },
        }
