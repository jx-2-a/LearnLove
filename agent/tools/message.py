"""
消息工具 — get_chat_history, check_new_messages, search_messages
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

import zstandard as zstd

from agent.protocol import ok, err
from agent.outputs import clip
from agent.tools._state import state

_zstd_dctx = zstd.ZstdDecompressor()

MSG_TYPES = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/引用/文件", 50: "VOIP",
    10000: "系统消息", 10002: "撤回",
    244813135921: "视频号", 25769803825: "文件",
}


# ===== 日期解析 =====

def _parse_date(date_str: str) -> tuple[float, float]:
    """将人类可读的日期字符串转为当天 [start_ts, end_ts] 的 Unix 时间戳。

    支持格式:
      - '2024-08-04' 或 '2024/08/04'
      - '8月4日' 或 '8月4号'（默认今年）
      - '今天', '昨天', '前天'
      - '3天前', '一周前'

    Returns:
        (day_start_ts, day_end_ts) — 当天 00:00:00 到 23:59:59 的时间戳
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    s = date_str.strip()

    # ISO 格式: '2024-08-04' 或 '2024/08/04'
    iso_match = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if iso_match:
        y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
        target = datetime(y, m, d)
        return (target.timestamp(),
                target.replace(hour=23, minute=59, second=59).timestamp())

    # 中文月日: '8月4日' 或 '8月4号'
    md_match = re.match(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?', s)
    if md_match:
        m, d = int(md_match.group(1)), int(md_match.group(2))
        target = datetime(now.year, m, d)
        if target > now:
            # 如果还没到，可能是去年
            target = datetime(now.year - 1, m, d)
        return (target.timestamp(),
                target.replace(hour=23, minute=59, second=59).timestamp())

    # 相对日期
    relative = {
        '今天': 0, '昨天': 1, '前天': 2,
        'today': 0, 'yesterday': 1,
    }
    if s in relative:
        days = relative[s]
        target = today_start - timedelta(days=days)
        return (target.timestamp(),
                target.replace(hour=23, minute=59, second=59).timestamp())

    # N天前 / 一周前
    n_days_match = re.match(r'(\d+)\s*天\s*前', s)
    if n_days_match:
        days = int(n_days_match.group(1))
        target = today_start - timedelta(days=days)
        return (target.timestamp(),
                target.replace(hour=23, minute=59, second=59).timestamp())

    if '一周' in s or '星期' in s:
        target = today_start - timedelta(days=7)
        return (target.timestamp(),
                today_start.replace(hour=23, minute=59, second=59).timestamp())

    raise ValueError(f"无法解析日期: {date_str!r}。支持格式: '2024-08-04', '8月4日', '今天', '昨天', '3天前'")


def _decode_text(content, ct: int) -> str:
    """解码消息内容：ZSTD 压缩或 UTF-8 文本"""
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return content.decode('utf-8', errors='replace')
    elif isinstance(content, bytes):
        return content.decode('utf-8', errors='replace')
    else:
        return str(content) if content else ""


def _parse_type49(xml_content: str) -> tuple[str, str]:
    """解析 type 49 消息的 XML 内容，区分引用/链接/文件/视频号等子类型。

    Returns: (类型标签, 摘要文本)
    """
    if not xml_content:
        return ("链接/文件", "[空内容]")

    # 提取 appmsg 中的关键字段
    title = ""
    des = ""
    app_type = ""

    # 提取 title（只取 <appmsg> 里的，避免误取 <refermsg> 里的 <title>）
    title_m = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', xml_content, re.DOTALL)
    if title_m:
        title = title_m.group(1).strip()

    # 提取 des
    des_m = re.search(r'<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>', xml_content, re.DOTALL)
    if des_m:
        des = des_m.group(1).strip()

    # 提取 appmsg type
    type_m = re.search(r'<type>(\d+)</type>', xml_content)
    if type_m:
        app_type = type_m.group(1)

    # ===== 检测 body 类型（在 refermsg 检查前确定，用于组合标签）=====
    body_is_finder = 'finderFeed' in xml_content
    body_is_file = (app_type == '6')
    body_is_link = bool(title) and not body_is_finder and not body_is_file

    # ===== 引用检测 =====
    # 多种 <refermsg> 格式：<refermsg>...</refermsg>、<refermsg .../>、<refermsg/>
    has_refermsg = bool(re.search(r'<refermsg\b', xml_content))

    if has_refermsg:
        # 提取 <refermsg> 整个块
        refermsg_block = ""
        ref_block_m = re.search(r'<refermsg\b.*?</refermsg>', xml_content, re.DOTALL)
        if ref_block_m:
            refermsg_block = ref_block_m.group(0)
        # 自闭合 <refermsg ... /> 没有内容可提取

        ref_content = ""
        ref_dn = ""
        ref_type = ""   # 被引用消息的类型

        if refermsg_block:
            # 引用内容
            ref_ct_m = re.search(
                r'<content>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</content>',
                refermsg_block, re.DOTALL
            )
            if ref_ct_m:
                ref_content = ref_ct_m.group(1).strip()

            # 引用者名称
            ref_dn_m = re.search(
                r'<displayname>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</displayname>',
                refermsg_block, re.DOTALL
            )
            if ref_dn_m:
                ref_dn = ref_dn_m.group(1).strip()

            # 被引用消息的类型（用于区分引用的是文本/图片/视频号等）
            ref_type_m = re.search(r'<type>\s*(\d+)\s*</type>', refermsg_block)
            if ref_type_m:
                ref_type = ref_type_m.group(1)

        # 构建引用摘要
        ref_parts = []
        ref_label = f"引用 {ref_dn}" if ref_dn else "引用"
        if ref_content:
            short = ref_content[:100] + ("..." if len(ref_content) > 100 else "")
            ref_parts.append(f"[{ref_label}] {short}")
        elif title:
            # 有些版本把引用内容放在 appmsg 的 title/des 里
            ref_parts.append(f"[{ref_label}] {title[:100]}")
        else:
            ref_parts.append(f"[{ref_label}]")

        # 构建正文摘要
        body_parts = []
        if title and ref_content:
            # title 是正文（不是引用内容）
            body_parts.append(title[:120])
        if des and (ref_content or (title and title != des)):
            body_parts.append(des[:120])
        if not body_parts and title and not ref_content:
            # title 已经作为引用内容了，des 可能是正文
            if des and des != title:
                body_parts.append(des[:120])

        # 组合标签：引用 + body 类型
        if body_is_finder:
            type_label = "引用(视频号)"
        elif body_is_file:
            type_label = "引用(文件)"
        elif body_is_link:
            type_label = "引用(链接)"
        else:
            type_label = "引用"

        # 组装最终摘要
        summary_parts = ref_parts
        if body_parts:
            summary_parts.append("[正文] " + " — ".join(body_parts))
        elif body_is_finder:
            summary_parts.append("[正文: 视频号分享]")

        return (type_label, " | ".join(summary_parts))

    # 文件类型（非引用）
    if body_is_file:
        filename = title or des or ""
        return ("文件", filename if filename else "[文件]")

    # 视频号分享（type 49 内的 finderFeed 标签，区别于独立 type 244813135921）
    if body_is_finder:
        return ("视频号", title or "[视频号分享]")

    # 链接/文章
    if title:
        summary = title
        if des:
            summary += f" — {des[:80]}"
        return ("链接", clip(summary, 300))

    # 兜底
    return ("链接/文件", title or des or "[链接/文件]")


def _format_message_body(m: dict) -> tuple[str, str]:
    """格式化单条消息的显示文本。

    Returns: (类型名, 显示文本)
    """
    lt = m["local_type"]
    content = m.get("content", "")

    if lt == 1:
        return (MSG_TYPES.get(lt, f"type={lt}"), content)
    elif lt == 34:
        vt = m.get("voice_text")
        if vt:
            return ("语音", vt)
        return ("语音", "[语音] (未转录)")
    elif lt == 47:
        return ("表情", "[表情]")
    elif lt == 3:
        return ("图片", "[图片]")
    elif lt == 43:
        return ("视频", "[视频]")
    elif lt == 48:
        return ("位置", "[位置]")
    elif lt == 49:
        sub_type, summary = _parse_type49(content)
        # sub_type 已经包含了完整分类，如 "引用", "引用(视频号)", "链接", "视频号" 等
        return (sub_type, summary)
    elif lt == 50:
        return ("VOIP", "[通话]")
    elif lt == 10000:
        return ("系统消息", content[:200] if content else "[系统消息]")
    elif lt == 10002:
        return ("撤回", "[消息已撤回]")
    elif lt == 244813135921:
        # 独立视频号类型 — 内容也可能是 XML，统一走 type49 解析器
        if content and content.strip().startswith('<'):
            sub_type, summary = _parse_type49(content)
            # 如果解析器没识别出视频号，覆盖为正确标签
            if '视频号' not in sub_type:
                sub_type = f"视频号({sub_type})" if sub_type != "引用" else "视频号(引用)"
            return (sub_type, summary)
        # 纯文本的视频号
        if content:
            title_m = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', content, re.DOTALL)
            if title_m:
                return ("视频号", title_m.group(1).strip()[:200])
        return ("视频号", content[:200] if content else "[视频号]")
    elif lt == 25769803825:
        return ("文件", content[:200] if content else "[文件]")
    else:
        type_name = MSG_TYPES.get(lt, f"type={lt}")
        # 未知类型但包含 appmsg XML → 尝试解析（引用、链接、小程序、转写等）
        if content and '<appmsg' in content:
            sub_type, summary = _parse_type49(content)
            return (f"{sub_type}", summary)
        return (type_name, content[:300] if content else f"[{type_name}]")


def _find_msg_table(wxid: str) -> list[tuple[str, str]]:
    """查找联系人对应的 Msg_ 表。返回 [(db_path, table_name), ...]"""
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
    """从消息 XML 中动态学习 sender_id → 名称的映射"""
    sender_map = {}
    try:
        rows = conn.execute(
            f"SELECT real_sender_id, message_content, WCDB_CT_message_content "
            f"FROM [{table_name}] WHERE local_type IN (34, 47) LIMIT 20"
        ).fetchall()
        for sid, content, ct in rows:
            text = _decode_text(content, ct)
            m = re.search(r'fromusername\s*=\s*"([^"]+)"', text)
            if m:
                from_user = m.group(1)
                if from_user == wxid:
                    sender_map[sid] = display_name
                else:
                    sender_map[sid] = "你"
    except Exception:
        pass
    return sender_map


def _get_sender_map(conn, table_name: str, wxid: str, display_name: str) -> dict:
    """获取或学习 sender_map，默认 {1: display_name, 2: '你'}"""
    # 先尝试从缓存获取
    if wxid in state.sender_maps and state.sender_maps[wxid]:
        return state.sender_maps[wxid]

    # 学习
    smap = _learn_sender_map(conn, table_name, wxid, display_name)
    if smap:
        state.sender_maps[wxid] = smap
        return smap

    # 默认
    default = {1: display_name, 2: "你"}
    state.sender_maps[wxid] = default
    return default


def _query_messages(conn, table_name: str, since_ts: float = 0,
                    before_ts: float = None, limit: int = 50) -> list[dict]:
    """查询消息，返回结构化列表。支持单独或组合时间范围。"""
    conditions = []
    params = []

    if since_ts > 0:
        conditions.append("create_time > ?")
        params.append(since_ts)
    if before_ts:
        conditions.append("create_time < ?")
        params.append(before_ts)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # 如果有上限时间无下限，倒序取最近的
    if before_ts and not (since_ts > 0):
        order = "ORDER BY create_time DESC"
        params.append(limit)
        sql = f"SELECT create_time, local_type, local_id, message_content, " \
              f"WCDB_CT_message_content, real_sender_id " \
              f"FROM [{table_name}] {where} {order} LIMIT ?"
    elif since_ts > 0 and not before_ts:
        order = "ORDER BY create_time ASC"
        sql = f"SELECT create_time, local_type, local_id, message_content, " \
              f"WCDB_CT_message_content, real_sender_id " \
              f"FROM [{table_name}] {where} {order}"
    elif since_ts > 0 and before_ts:
        # 时间范围查询：正序，可限制条数
        order = "ORDER BY create_time DESC"
        params.append(limit)
        sql = f"SELECT create_time, local_type, local_id, message_content, " \
              f"WCDB_CT_message_content, real_sender_id " \
              f"FROM [{table_name}] {where} {order} LIMIT ?"
    else:
        order = "ORDER BY create_time DESC"
        params.append(limit)
        sql = f"SELECT create_time, local_type, local_id, message_content, " \
              f"WCDB_CT_message_content, real_sender_id " \
              f"FROM [{table_name}] {order} LIMIT ?"

    rows = conn.execute(sql, params).fetchall()

    messages = []
    for ts, lt, lid, content, ct, sid in rows:
        text = _decode_text(content, ct)
        messages.append({
            "create_time": ts,
            "local_type": lt,
            "local_id": lid,
            "sender_id": sid,
            "content": text,
            "voice_text": None,
        })
    return messages


# ===== 工具函数 =====


def get_chat_history(contact_name: str, limit: int = 30,
                     date: str = None, since_ts: float = None,
                     before_ts: float = None) -> dict:
    """获取与联系人的聊天记录

    Args:
        contact_name: 联系人名称
        limit: 返回消息条数，默认 30，最多 200
        date: 人类可读日期，如 '2024-08-04', '8月4日', '今天', '昨天'
              (自动转为当天 00:00–23:59 范围，覆盖 since_ts/before_ts)
        since_ts: Unix 时间戳，只返回此时间之后的消息
        before_ts: Unix 时间戳，只返回此时间之前的消息

    Returns {ok, data: {messages: [{time_str, type, type_name, sender, content, voice_text}, ...]}}
    """
    wxid, display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return err(f"未找到联系人: {contact_name}")

    # 解析日期字符串
    if date:
        try:
            day_start, day_end = _parse_date(date)
            since_ts = day_start
            before_ts = day_end
        except ValueError as e:
            return err(str(e))

    limit = min(limit, 200)

    tables = _find_msg_table(wxid)
    if not tables:
        return err(f"未找到 {display} 的消息表")

    all_msgs = []
    for db_path, table_name in tables:
        try:
            conn = sqlite3.connect(db_path)
        except Exception:
            continue

        sender_map = _get_sender_map(conn, table_name, wxid, display)
        msgs = _query_messages(conn, table_name,
                               since_ts=since_ts or 0,
                               before_ts=before_ts,
                               limit=limit)
        conn.close()

        for m in msgs:
            ts_str = datetime.fromtimestamp(m["create_time"]).strftime("%m-%d %H:%M")
            type_name, body = _format_message_body(m)
            sender = sender_map.get(m["sender_id"], f"sid={m['sender_id']}")

            all_msgs.append({
                "time": ts_str,
                "create_time": m["create_time"],
                "type": type_name,
                "sender": sender,
                "content": clip(body, 500),
            })

    # 去重 + 排序（默认倒序）
    seen = set()
    unique = []
    for m in all_msgs:
        key = (m["create_time"], m["sender"], m["content"][:100])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    unique.sort(key=lambda m: m["create_time"], reverse=True)
    result = unique[:limit]

    return ok({"contact": display, "messages": result, "count": len(result),
               "query": {"date": date, "since_ts": since_ts, "before_ts": before_ts}})


def check_new_messages(contact_name: str = None) -> dict:
    """检查新消息。刷新解密后查询 since_ts 之后的消息。

    Returns {ok, data: {contact: str, messages: [...], new_since_ts: int}}
    """
    # 如指定联系人则查指定的，否则查所有已配置的
    if contact_name:
        wxid, display = state.resolve_contact_exact(contact_name)
        if not wxid:
            return err(f"未找到联系人: {contact_name}")
        wxids = [wxid]
        displays = {wxid: display}
    else:
        wxids = [c["wxid"] for c in state.contacts_config()]
        displays = {}
        for wxid in wxids:
            if wxid in state.contacts:
                displays[wxid] = state.contacts[wxid]["display"]
            else:
                displays[wxid] = wxid

    stored_state = state.load_state()
    all_new = []

    for wxid in wxids:
        state_key = f"last_ts_{wxid}"
        since_ts = stored_state.get(state_key, 0)
        display = displays.get(wxid, wxid)

        tables = _find_msg_table(wxid)
        if not tables:
            continue

        for db_path, table_name in tables:
            try:
                conn = sqlite3.connect(db_path)
            except Exception:
                continue

            smap = _get_sender_map(conn, table_name, wxid, display)
            msgs = _query_messages(conn, table_name, since_ts=since_ts)
            conn.close()

            for m in msgs:
                ts_str = datetime.fromtimestamp(m["create_time"]).strftime("%m-%d %H:%M")
                type_name, body = _format_message_body(m)
                sender = smap.get(m["sender_id"], f"sid={m['sender_id']}")

                all_new.append({
                    "time": ts_str,
                    "create_time": m["create_time"],
                    "wxid": wxid,
                    "contact": display,
                    "type": type_name,
                    "local_type": m["local_type"],
                    "sender": sender,
                    "sender_id": m["sender_id"],
                    "content": body,
                    "raw_content": m["content"],
                    "voice_text": m.get("voice_text"),
                })

    # 更新状态
    if all_new:
        for wxid in wxids:
            wx_msgs = [m for m in all_new if m["wxid"] == wxid]
            if wx_msgs:
                max_ts = max(m["create_time"] for m in wx_msgs)
                stored_state[f"last_ts_{wxid}"] = max_ts
        state.save_state(stored_state)

    # 按时间排序
    all_new.sort(key=lambda m: m["create_time"])

    return ok({
        "messages": all_new,
        "count": len(all_new),
        "contacts_checked": len(wxids),
    })


def search_messages(keyword: str, contact_name: str = None,
                    limit: int = 20) -> dict:
    """跨所有消息数据库搜索关键词

    Returns {ok, data: {results: [...], total: int}}
    """
    results = []
    wxid_filter = None
    if contact_name:
        wxid_filter, _ = state.resolve_contact_exact(contact_name)

    for rel_key in state.db_cache.get_msg_db_keys():
        db_path = state.db_cache.get(rel_key)
        if not db_path:
            continue
        try:
            conn = sqlite3.connect(db_path)
        except Exception:
            continue

        # 查找所有 Msg_ 表
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()
        for (tname,) in tables:
            # 如果指定了联系人，检查表名是否匹配
            if wxid_filter:
                expected_hash = hashlib.md5(wxid_filter.encode()).hexdigest()
                expected_table = f"Msg_{expected_hash}"
                if tname != expected_table:
                    continue

            try:
                rows = conn.execute(
                    f"SELECT create_time, local_type, message_content, "
                    f"WCDB_CT_message_content, real_sender_id "
                    f"FROM [{tname}] WHERE message_content LIKE ? "
                    f"ORDER BY create_time DESC LIMIT ?",
                    (f"%{keyword}%", limit)
                ).fetchall()
            except Exception:
                continue

            for ts, lt, content, ct, sid in rows:
                text = _decode_text(content, ct)
                # 提取匹配片段
                idx = text.lower().find(keyword.lower())
                start = max(0, idx - 30)
                end = min(len(text), idx + len(keyword) + 30)
                snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")

                ts_str = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                results.append({
                    "time": ts_str,
                    "create_time": ts,
                    "type": MSG_TYPES.get(lt, f"type={lt}"),
                    "snippet": snippet,
                })

            if len(results) >= limit:
                break

        conn.close()
        if len(results) >= limit:
            break

    results.sort(key=lambda r: r["create_time"], reverse=True)
    results = results[:limit]

    return ok({"results": results, "total": len(results)})


def get_live_feed(contact_name: str = "", mark_read: bool = True) -> dict:
    """读取实时消息流 (data/live/incoming.jsonl)

    Returns {ok, data: {messages: [...]}}
    """
    feed_path = state.live_feed_path
    if not os.path.exists(feed_path):
        return ok({"messages": [], "note": "尚无监听消息"})

    lines = []
    with open(feed_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if contact_name and contact_name not in entry.get("sender", ""):
                    continue
                lines.append(entry)
            except json.JSONDecodeError:
                continue

    return ok({"messages": lines[-50:], "total_lines": len(lines)})
