"""事件/故事留存工具。"""

from agent.archive import list_events, record_event as save_event
from agent.protocol import err, ok
from agent.tools._state import state


def record_event(title: str, summary: str, narrative: str = "",
                 contact_name: str = "", event_time: str = "",
                 participants: list[str] = None, facts: list[str] = None,
                 emotions: list[str] = None, uncertainties: list[str] = None,
                 tags: list[str] = None, source_message_ids: list[str] = None,
                 event_id: str = "") -> dict:
    """保存事件全貌；传 event_id 时形成有历史版本的修订。"""
    if not summary.strip() and not narrative.strip():
        return err("事件摘要或完整叙述至少填写一项")
    name = contact_name or state.active_contact_name or "自己"
    result = save_event(name,title,summary or narrative[:200],narrative,event_time,
                        participants,facts,emotions,uncertainties,tags,
                        source_message_ids,event_id)
    return ok({"contact":name,**result})


def view_events(contact_name: str = "", keyword: str = "", limit: int = 10) -> dict:
    """按联系人或关键词查看已留存事件。"""
    name = contact_name or state.active_contact_name
    events = list_events(contact_name=name,keyword=keyword,limit=limit)
    return ok({"contact":name or "全部","events":events,"count":len(events)})
