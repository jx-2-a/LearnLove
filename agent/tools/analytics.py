"""长期微信消息统计工具。"""

from agent.message_analytics import analyze_message_statistics, get_message_rollup as build_message_rollup
from agent.protocol import err, ok
from agent.tools._state import state


def get_message_statistics(contact_name: str = "", days: int = 0,
                           end_time: float = 0, granularity: str = "day",
                           include_buckets: bool = False) -> dict:
    """返回相邻时段的消息数量、形式、短语和变化，不做读心结论。"""
    try:
        return ok(analyze_message_statistics(
            contact_name or state.active_contact_name or "自己", days, end_time, granularity, include_buckets,
        ))
    except (TypeError, ValueError) as exc:
        return err(str(exc))


def get_message_rollup(contact_name: str = "", level: str = "auto",
                       start_time: float = 0, end_time: float = 0, limit: int = 36) -> dict:
    """返回紧凑的月/周/日消息时间轴，不携带聊天原文。"""
    try:
        return ok(build_message_rollup(contact_name or state.active_contact_name or "自己", level, start_time, end_time, limit))
    except (TypeError, ValueError) as exc:
        return err(str(exc))
