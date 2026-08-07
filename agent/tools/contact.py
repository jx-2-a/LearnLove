"""
联系人工具 — find_contact, list_contacts
"""

import sqlite3
from agent.protocol import ok, err
from agent.tools._state import state


def find_contact(query: str) -> dict:
    """查找联系人。支持昵称、备注、别名、wxid 模糊搜索。

    Returns {ok: true, data: {wxid, display_name, nick, remark, alias}}
    """
    if not state.contacts:
        return err("联系人数据未加载，请先初始化")

    wxid, display = state.resolve_contact(query)
    if not wxid:
        return err(f"未找到匹配 '{query}' 的联系人")

    info = state.contacts[wxid]
    return ok({
        "wxid": wxid,
        "display_name": display,
        "nick": info["nick"],
        "remark": info["remark"],
        "alias": info["alias"],
    })


def list_contacts(limit: int = 30) -> dict:
    """列出所有联系人

    Returns {ok: true, data: {contacts: [...], total: int}}
    """
    if not state.contacts:
        return err("联系人数据未加载，请先初始化")

    # 尝试获取每个联系人的最近消息时间
    contacts_list = []
    for wxid, info in list(state.contacts.items())[:limit]:
        contacts_list.append({
            "wxid": wxid,
            "display_name": info["display"],
            "nick": info["nick"],
            "remark": info.get("remark", ""),
        })

    return ok({
        "contacts": contacts_list,
        "total": len(state.contacts),
        "shown": len(contacts_list),
    })


def switch_contact(contact_name: str) -> dict:
    """切换当前活跃联系人

    Returns {ok: true, data: {wxid, display_name, previous}}
    """
    if not state.contacts:
        return err("联系人数据未加载")

    wxid, display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return err(f"未找到联系人: {contact_name}")

    previous = state.active_contact_name
    state.active_contact_wxid = wxid
    state.active_contact_name = display

    return ok({
        "wxid": wxid,
        "display_name": display,
        "previous": previous,
    })
