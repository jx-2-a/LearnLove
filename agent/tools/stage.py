"""阶段分析底稿工具。"""

from agent.protocol import ok
from agent.stage_reports import save_stage_report, list_stage_reports, read_stage_report
from agent.tools._state import state


def record_stage_analysis(title: str, start: str, end: str, facts: list[str], interpretation: str = "", uncertainties: list[str] = None, next_check: str = "", contact_name: str = "", stage_id: str = "", revision_reason: str = "") -> dict:
    """保存可复核的阶段分析；事实与解释强制分栏。"""
    return ok(save_stage_report(contact_name or state.active_contact_name or "自己", title, start, end, facts, interpretation, uncertainties, next_check, stage_id, revision_reason))


def view_stage_analyses(contact_name: str = "", limit: int = 20) -> dict:
    """查看已有阶段分析底稿。"""
    name = contact_name or state.active_contact_name or "自己"
    return ok({"contact": name, "reports": list_stage_reports(name, limit)})


def read_stage_analysis(stage_id: str, contact_name: str = "") -> dict:
    """读取一个阶段的当前版本与修订历史。"""
    name = contact_name or state.active_contact_name or "自己"
    try: return ok(read_stage_report(name, stage_id))
    except FileNotFoundError as exc: return {"ok": False, "error": str(exc)}
