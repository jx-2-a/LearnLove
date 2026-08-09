"""
后台监听守护线程 — 轮询 WeChat DB，检测新消息

复用 agent/tools/message.py 的查询逻辑，
将新消息推送到 state.monitor_queue 并写入 incoming.jsonl。
"""

import os
import json
import time
import threading
import hashlib
import sqlite3
import re
from datetime import datetime

import zstandard as zstd

from agent.tools._state import state

_zstd_dctx = zstd.ZstdDecompressor()

MSG_TYPES = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/文件", 50: "VOIP",
    10000: "系统消息", 10002: "撤回",
}


def _decode_text(content, ct: int) -> str:
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return content.decode('utf-8', errors='replace')
    elif isinstance(content, bytes):
        return content.decode('utf-8', errors='replace')
    return str(content) if content else ""


def _find_msg_table(wxid: str) -> list[tuple[str, str]]:
    """查找联系人对应的 Msg_ 表"""
    table_hash = hashlib.md5(wxid.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    results = []
    for rel_key in state.db_cache.get_msg_db_keys():
        db_path = state.db_cache.get(rel_key)
        if not db_path:
            continue
        try:
            conn = sqlite3.connect(db_path)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if exists:
                results.append((db_path, table_name))
            conn.close()
        except Exception:
            pass
    return results


def _learn_sender_map(conn, table_name: str, wxid: str, display_name: str) -> dict:
    """从消息 XML 中学习 real_sender_id → 名称 的映射。

    逐层尝试：语音/表情 → 链接/文件 → 全表搜索 fromusername。
    避免因为某类消息缺失就 fallback 到错误的 {1, 2} 假设。
    """
    sender_map = {}

    def _try_learn(msg_types: list[int], limit: int = 100) -> bool:
        """尝试从指定类型的消息中学习映射。返回 True 表示学到了至少一个。"""
        if not msg_types:
            return False
        placeholders = ",".join("?" * len(msg_types))
        try:
            rows = conn.execute(
                f"SELECT real_sender_id, message_content, WCDB_CT_message_content "
                f"FROM [{table_name}] WHERE local_type IN ({placeholders}) "
                f"ORDER BY create_time DESC LIMIT {limit}",
                msg_types
            ).fetchall()
            for sid, content, ct in rows:
                if sid in sender_map:
                    continue
                text = _decode_text(content, ct)
                m = re.search(r'fromusername\s*=\s*"([^"]+)"', text)
                if m:
                    from_user = m.group(1)
                    sender_map[sid] = display_name if from_user == wxid else "你"
            return len(sender_map) > 0
        except Exception:
            return False

    # 第一层：语音(34) + 表情(47) — XML 最规整
    if _try_learn([34, 47], limit=50):
        return sender_map

    # 第二层：链接/文件(49) + 引用(244813135921) — 也有 XML
    if _try_learn([49, 244813135921], limit=50):
        return sender_map

    # 第三层：全表搜 fromusername（不限消息类型）
    try:
        rows = conn.execute(
            f"SELECT real_sender_id, message_content, WCDB_CT_message_content "
            f"FROM [{table_name}] WHERE message_content LIKE '%fromusername%' "
            f"ORDER BY create_time DESC LIMIT 200"
        ).fetchall()
        for sid, content, ct in rows:
            if sid in sender_map:
                continue
            text = _decode_text(content, ct)
            m = re.search(r'fromusername\s*=\s*"([^"]+)"', text)
            if m:
                from_user = m.group(1)
                sender_map[sid] = display_name if from_user == wxid else "你"
    except Exception:
        pass

    return sender_map


def _monitor_loop(wxids: list[str], interval: float = 2.0):
    """后台监听主循环"""
    stored_state = state.load_state()
    sender_maps = {}  # wxid -> {sid: name}

    while state.monitor_running:
        try:
            for wxid in wxids:
                display = state.contacts.get(wxid, {}).get("display", wxid)
                state_key = f"last_ts_{wxid}"
                since_ts = stored_state.get(state_key, 0)

                tables = _find_msg_table(wxid)
                if not tables:
                    continue

                for db_path, table_name in tables:
                    try:
                        conn = sqlite3.connect(db_path)
                    except Exception:
                        continue

                    # 学习/获取 sender_map
                    if wxid not in sender_maps:
                        smap = _learn_sender_map(conn, table_name, wxid, display)
                        if not smap:
                            # 学不到就标记为「对方」和「你」，比猜错 {1,2} 好
                            # 至少消息不会全显示成同一个人
                            smap = {}
                        sender_maps[wxid] = smap
                    smap = sender_maps[wxid]

                    # 查询新消息
                    try:
                        rows = conn.execute(
                            f"SELECT create_time, local_type, local_id, message_content, "
                            f"WCDB_CT_message_content, real_sender_id "
                            f"FROM [{table_name}] WHERE create_time > ? "
                            f"ORDER BY create_time ASC",
                            (since_ts,)
                        ).fetchall()
                    except Exception:
                        conn.close()
                        continue

                    conn.close()

                    for ts, lt, lid, content, ct, sid in rows:
                        text = _decode_text(content, ct)
                        sender = smap.get(sid, f"sid={sid}")
                        ts_str = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")

                        # 格式化 — 复用 message.py 的逻辑，正确解析引用/链接/引用等
                        from agent.tools.message import _format_message_body
                        msg_for_format = {
                            "local_type": lt,
                            "content": text,
                            "voice_text": None,
                        }
                        type_name, body = _format_message_body(msg_for_format)

                        entry = {
                            "time": ts_str,
                            "create_time": ts,
                            "wxid": wxid,
                            "sender": sender,
                            "sender_id": sid,
                            "type": lt,
                            "type_name": type_name,
                            "content": body,
                            "raw_content": text,
                            "voice_text": None,
                        }

                        # 推送到队列
                        state.monitor_queue.put(entry)

                        # 写入 feed 文件
                        _append_feed(entry)

                        # 更新状态
                        since_ts = max(since_ts, ts)

                    # 保存状态
                    stored_state[state_key] = since_ts
                    state.save_state(stored_state)

        except Exception as e:
            print(f"[monitor] 轮询异常: {e}")

        time.sleep(interval)


def _append_feed(entry: dict):
    """追加到 incoming.jsonl"""
    feed_path = state.live_feed_path
    os.makedirs(os.path.dirname(feed_path), exist_ok=True)
    feed_entry = {
        "time": entry["time"],
        "sender": entry["sender"],
        "sender_id": entry["sender_id"],
        "type": entry["type"],
        "type_name": entry["type_name"],
        "content": entry["content"],
        "voice_text": entry.get("voice_text"),
    }
    # 过滤 None
    feed_entry = {k: v for k, v in feed_entry.items() if v is not None}
    with open(feed_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(feed_entry, ensure_ascii=False) + "\n")


def _start_monitoring_raw(contacts: list[str], interval: float = 2.0) -> threading.Thread:
    """启动后台监听线程

    Args:
        contacts: 要监听的 wxid 列表
        interval: 轮询间隔（秒）

    Returns:
        daemon 线程
    """
    state.monitor_running = True
    t = threading.Thread(
        target=_monitor_loop,
        args=(contacts, interval),
        daemon=True,
        name="wechat-monitor",
    )
    t.start()
    return t


def stop_monitoring():
    """停止后台监听"""
    state.monitor_running = False


def check_monitor_status() -> dict:
    """检查监听状态"""
    return {
        "ok": True,
        "data": {
            "running": state.monitor_running,
            "thread_alive": state.monitor_thread.is_alive() if state.monitor_thread else False,
            "active_contact": state.active_contact_name,
            "queue_size": state.monitor_queue.qsize(),
        },
    }
