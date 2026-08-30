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
    244813135921: "引用", 25769803825: "文件",
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

    # 中文年月: '2024年6月' / '2024年06月' → 当月 1 号 ~ 月末
    ym_match = re.match(r'(\d{4})\s*年\s*(\d{1,2})\s*月', s)
    if ym_match:
        y, m = int(ym_match.group(1)), int(ym_match.group(2))
        if not (1 <= m <= 12):
            raise ValueError(f"无效月份: {m}")
        start = datetime(y, m, 1)
        end = datetime(y, 12, 31) if m == 12 else datetime(y, m + 1, 1) - timedelta(days=1)
        return (start.timestamp(),
                end.replace(hour=23, minute=59, second=59).timestamp())

    # 中文年份: '2024年' → 整年
    y_match = re.match(r'(\d{4})\s*年', s)
    if y_match:
        y = int(y_match.group(1))
        start = datetime(y, 1, 1)
        end = datetime(y, 12, 31, 23, 59, 59)
        return (start.timestamp(), end.timestamp())

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

    # 去年 / 前年 → 整年
    if s in ('去年', '前年'):
        y = now.year - (1 if s == '去年' else 2)
        start = datetime(y, 1, 1)
        end = datetime(y, 12, 31, 23, 59, 59)
        return (start.timestamp(), end.timestamp())

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

    raise ValueError(f"无法解析日期: {date_str!r}。支持格式: '2024-08-04', '2024年6月', '2024年', '8月4日', '今天', '昨天', '去年', '3天前'")


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
    """解析 type 49 消息的 XML 内容，区分引用/链接/文件/引用等子类型。

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

            # 被引用消息的类型（用于区分引用的是文本/图片/引用等）
            ref_type_m = re.search(r'<type>\s*(\d+)\s*</type>', refermsg_block)
            if ref_type_m:
                ref_type = ref_type_m.group(1)

        # 构建正文摘要（实际新发的消息内容）
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

        body_text = " — ".join(body_parts)
        if not body_text and body_is_finder:
            body_text = "引用分享"

        # 被引用内容只作短上下文（真正的对话是上面的正文）
        ref_text = ""
        if ref_content:
            ref_text = ref_content
        elif title and not body_parts:
            # 有些版本把引用内容放在 appmsg 的 title/des 里
            ref_text = title
        # 引用内容是原始 msg-id（图片/表情被引用时形如 wxid_xxx:1719...::0），非文本不展示
        if re.match(r'^\S+:\d+:\d+:[^:]*::\d+$', ref_text):
            ref_text = ""

        # 组装最终摘要：正文在前，被引用内容降级为短尾巴
        if body_text:
            if ref_text:
                short_ref = ref_text[:60] + ("..." if len(ref_text) > 60 else "")
                return ("引用", f"{body_text} [引用: {short_ref}]")
            return ("引用", body_text)
        if ref_text:
            return ("引用", f"[引用] {ref_text[:80]}")
        return ("引用", f"[引用 {ref_dn}]" if ref_dn else "[引用]")

    # 文件类型（非引用）
    if body_is_file:
        filename = title or des or ""
        return ("文件", filename if filename else "[文件]")

    # 引用分享（type 49 内的 finderFeed 标签，区别于独立 type 244813135921）
    if body_is_finder:
        return ("引用", title or "[引用分享]")

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
        # sub_type 已经包含了完整分类，如 "引用", "引用(引用)", "链接", "引用" 等
        return (sub_type, summary)
    elif lt == 50:
        return ("VOIP", "[通话]")
    elif lt == 10000:
        return ("系统消息", content[:200] if content else "[系统消息]")
    elif lt == 10002:
        return ("撤回", "[消息已撤回]")
    elif lt == 244813135921:
        # 独立引用类型 — 内容也可能是 XML，统一走 type49 解析器
        if content and content.strip().startswith('<'):
            sub_type, summary = _parse_type49(content)
            # 如果解析器没识别出引用，覆盖为正确标签
            if '引用' not in sub_type:
                sub_type = f"引用({sub_type})" if sub_type != "引用" else "引用(引用)"
            return (sub_type, summary)
        # 纯文本的引用
        if content:
            title_m = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', content, re.DOTALL)
            if title_m:
                return ("引用", title_m.group(1).strip()[:200])
        return ("引用", content[:200] if content else "[引用]")
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
    """从消息 XML 中学习 real_sender_id → 名称 的映射。

    逐层尝试：语音/表情 → 链接/文件 → 全表搜索 fromusername。
    避免因为某类消息缺失就 fallback 到错误的 {1, 2} 假设。
    """
    sender_map = {}

    def _try_learn(msg_types: list[int], limit: int = 100) -> bool:
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


def _get_sender_map(conn, table_name: str, wxid: str, display_name: str,
                    db_path: str = "") -> dict:
    """获取或学习 sender_map。学不到就返回空 map（由调用方按 sid=X 显示），
    不再假设 {1: 联系人, 2: 你}——那个假设在很多数据库中不成立。
    """
    # 按 (wxid, db_path) 分别缓存：不同 message_N.db 里的 real_sender_id 编码可能不同
    #（如新库用 1/2，老库用 2/3），不能全局共用一个 map
    key = (wxid, db_path)
    cached = state.sender_maps.get(key, None)
    if cached is not None:
        return cached

    # 学习
    smap = _learn_sender_map(conn, table_name, wxid, display_name)
    state.sender_maps[key] = smap  # 缓存结果（即便是空 map）
    return smap


def _query_messages(conn, table_name: str, since_ts: float = 0,
                    before_ts: float = None, limit: int = 50,
                    oldest: bool = False) -> list[dict]:
    """查询消息，返回结构化列表。支持单独或组合时间范围。

    oldest=True 时按时间正序取最早 limit 条（查看相识初期等早期记录）。
    """
    conditions = []
    params = []

    if since_ts > 0:
        conditions.append("create_time > ?")
        params.append(since_ts)
    if before_ts:
        conditions.append("create_time < ?")
        params.append(before_ts)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    select = f"SELECT create_time, local_type, local_id, message_content, " \
             f"WCDB_CT_message_content, real_sender_id FROM [{table_name}]"

    # check_new_messages 依赖：只给 since_ts 时正序拉全量（不设 LIMIT）
    if since_ts > 0 and not before_ts and not oldest:
        sql = f"{select} {where} ORDER BY create_time ASC"
    else:
        # 默认倒序取最近 limit 条；oldest=True 时正序取最早 limit 条
        order = "ORDER BY create_time ASC" if oldest else "ORDER BY create_time DESC"
        params.append(limit)
        sql = f"{select} {where} {order} LIMIT ?"

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
                     before_ts: float = None, oldest: bool = False) -> dict:
    """获取与联系人的聊天记录

    Args:
        contact_name: 联系人名称
        limit: 返回消息条数，默认 30，最多 200
        date: 人类可读日期，如 '2024-08-04', '2024年6月', '2024年', '8月4日',
              '今天', '昨天', '去年'
              (自动转为当天/当月/当年 时间范围，覆盖 since_ts/before_ts)
        since_ts: Unix 时间戳，只返回此时间之后的消息
        before_ts: Unix 时间戳，只返回此时间之前的消息
        oldest: 为 true 时返回最早的一批消息（默认倒序返回最近的消息）

    Returns {ok, data: {messages: [{time_str, type, type_name, sender, content, voice_text}, ...],
                        range: {earliest_ts, latest_ts, total, earliest, latest}}}
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

    now = datetime.now()
    all_msgs = []
    range_min = None
    range_max = None
    range_total = 0
    for db_path, table_name in tables:
        try:
            conn = sqlite3.connect(db_path)
        except Exception:
            continue

        try:
            # 统计该表的历史范围（尽早/最晚/总数），供调用方感知可查询的跨度
            r = conn.execute(
                f"SELECT MIN(create_time), MAX(create_time), COUNT(*) FROM [{table_name}]"
            ).fetchone()
            if r and r[2]:
                range_total += r[2]
                if r[0] is not None:
                    range_min = r[0] if range_min is None else min(range_min, r[0])
                if r[1] is not None:
                    range_max = r[1] if range_max is None else max(range_max, r[1])
        except Exception:
            pass

        sender_map = _get_sender_map(conn, table_name, wxid, display, db_path)
        msgs = _query_messages(conn, table_name,
                               since_ts=since_ts or 0,
                               before_ts=before_ts,
                               limit=limit,
                               oldest=oldest)
        conn.close()

        for m in msgs:
            # 跨年份时带上年份，避免早年记录与当年记录混淆
            ts_dt = datetime.fromtimestamp(m["create_time"])
            if ts_dt.year != now.year:
                ts_str = ts_dt.strftime("%Y-%m-%d %H:%M")
            else:
                ts_str = ts_dt.strftime("%m-%d %H:%M")
            type_name, body = _format_message_body(m)
            sender = sender_map.get(m["sender_id"], f"sid={m['sender_id']}")

            all_msgs.append({
                "time": ts_str,
                "create_time": m["create_time"],
                "type": type_name,
                "sender": sender,
                "content": clip(body, 500),
            })

    # 去重 + 排序（默认倒序取最近，oldest=True 正序取最早）
    seen = set()
    unique = []
    for m in all_msgs:
        key = (m["create_time"], m["sender"], m["content"][:100])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    unique.sort(key=lambda m: m["create_time"], reverse=not oldest)
    result = unique[:limit]

    range_info = {}
    if range_min is not None and range_max is not None:
        range_info = {
            "earliest_ts": range_min,
            "latest_ts": range_max,
            "total": range_total,
            "earliest": datetime.fromtimestamp(range_min).strftime("%Y-%m-%d"),
            "latest": datetime.fromtimestamp(range_max).strftime("%Y-%m-%d"),
        }

    return ok({"contact": display, "messages": result, "count": len(result),
               "oldest": oldest,
               "range": range_info,
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

            smap = _get_sender_map(conn, table_name, wxid, display, db_path)
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
