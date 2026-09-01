"""后台监听守护线程：与历史查询共用同一套微信规范化解析。"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from agent.archive import media_results_for_messages, upsert_messages
from agent.media import archive_message_media
from agent.tools._state import state
from agent.tools.message import (
    _find_msg_table,
    _get_sender_map,
    _query_messages,
)
from agent.wechat_parser import normalize_message


def _collect_contact_messages(wxid: str, display: str, stored_state: dict) -> list[dict]:
    """收集一个联系人的增量消息，并用稳定 ID 处理同秒边界与跨库重复。"""
    state_key = f"last_ts_{wxid}"
    since_ts = stored_state.get(state_key, 0)
    boundary_ids = set(stored_state.get(f"last_ids_{wxid}", []))
    collected = []

    for db_path, table_name, source_db in _find_msg_table(wxid):
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error:
            continue
        try:
            sender_map = _get_sender_map(conn, table_name, wxid, display, db_path)
            rows = _query_messages(
                conn,
                table_name,
                since_ts=max(0, since_ts - 1),
            )
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()

        for raw in rows:
            message = normalize_message(
                raw,
                wxid,
                display,
                sender_map,
                source_db,
                table_name,
            )
            if message["create_time"] < since_ts:
                continue
            if (
                message["create_time"] == since_ts
                and message["message_id"] in boundary_ids
            ):
                continue
            message["time"] = datetime.fromtimestamp(
                message["create_time"]
            ).strftime("%m-%d %H:%M")
            message["wxid"] = wxid
            message["contact"] = display
            collected.append(message)

    unique = {message["message_id"]: message for message in collected}
    messages = sorted(
        unique.values(),
        key=lambda message: (
            message["create_time"],
            message.get("sort_seq", 0),
            message.get("server_seq", 0),
            message.get("local_id", 0),
            message["message_id"],
        ),
    )
    if not messages:
        return []

    upsert_messages(messages)
    for message in messages:
        archive_message_media(message)
    media = media_results_for_messages(
        [message["message_id"] for message in messages]
    )
    for message in messages:
        if message["message_id"] in media:
            message["media"] = media[message["message_id"]]

    max_ts = max(message["create_time"] for message in messages)
    stored_state[state_key] = max_ts
    stored_state[f"last_ids_{wxid}"] = [
        message["message_id"]
        for message in messages
        if message["create_time"] == max_ts
    ]
    return messages


def _monitor_loop(wxids: list[str], interval: float = 2.0):
    """轮询联系人消息表，把统一事实对象推送给自动回复和实时记录。"""
    stored_state = state.load_state()
    while state.monitor_running:
        try:
            changed = False
            for wxid in wxids:
                display = state.contacts.get(wxid, {}).get("display", wxid)
                for entry in _collect_contact_messages(wxid, display, stored_state):
                    state.monitor_queue.put(entry)
                    _append_feed(entry)
                    changed = True
            if changed:
                state.save_state(stored_state)
        except Exception as exc:
            print(f"[monitor] 轮询异常: {exc}")
        time.sleep(interval)


def _append_feed(entry: dict):
    """追加规范化实时消息；保留角色、证据 ID、引用与媒体状态。"""
    feed_path = state.live_feed_path
    os.makedirs(os.path.dirname(feed_path), exist_ok=True)
    feed_entry = {
        "message_id": entry.get("message_id", ""),
        "time": entry.get("time", ""),
        "create_time": entry.get("create_time", 0),
        "sender": entry.get("sender", "未知发送者"),
        "sender_role": entry.get("sender_role", "unknown"),
        "sender_id": entry.get("sender_id"),
        "local_type": entry.get("local_type", 0),
        "type": entry.get("type", "未知"),
        "content": entry.get("content", ""),
        "quote": entry.get("quote") or None,
        "media": entry.get("media") or None,
    }
    feed_entry = {key: value for key, value in feed_entry.items() if value is not None}
    with open(feed_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(feed_entry, ensure_ascii=False) + "\n")


def _start_monitoring_raw(contacts: list[str], interval: float = 2.0) -> threading.Thread:
    """启动后台监听线程。"""
    state.monitor_running = True
    thread = threading.Thread(
        target=_monitor_loop,
        args=(contacts, interval),
        daemon=True,
        name="wechat-monitor",
    )
    thread.start()
    return thread


def stop_monitoring():
    """停止后台监听。"""
    state.monitor_running = False


def check_monitor_status() -> dict:
    """检查监听状态。"""
    return {
        "ok": True,
        "data": {
            "running": state.monitor_running,
            "thread_alive": (
                state.monitor_thread.is_alive() if state.monitor_thread else False
            ),
            "active_contact": state.active_contact_name,
            "queue_size": state.monitor_queue.qsize(),
        },
    }
