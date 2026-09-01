"""关系信号与趋势仪表盘工具。"""

from agent.archive import record_relationship_signal, relationship_dashboard
from agent.protocol import err, ok
from agent.tools._state import state


def record_relationship_signal_tool(dimension: str, direction: int,
                                    evidence: list[str], alternatives: list[str] = None,
                                    trigger_text: str = "", recommended_action: str = "",
                                    confidence: str = "low", contact_name: str = "",
                                    observed_at: str = "", source_message_ids: list[str] = None) -> dict:
    """保存有证据和替代解释的关系信号，避免把推断写成事实。"""
    try:
        result = record_relationship_signal(
            contact_name or state.active_contact_name or "自己", dimension, direction,
            evidence, alternatives, trigger_text, recommended_action, confidence,
            observed_at, source_message_ids,
        )
        return ok(result)
    except (TypeError, ValueError) as exc:
        return err(str(exc))


def get_relationship_dashboard(contact_name: str = "", limit: int = 30) -> dict:
    """读取关系趋势、预警与原始证据，不输出虚假的单一好感分。"""
    return ok(relationship_dashboard(contact_name or state.active_contact_name or "自己", limit))
