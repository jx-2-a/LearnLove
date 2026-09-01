"""基于统一原始消息归档的长期统计与趋势分析。"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from agent.archive import connect


PHRASE_GROUPS = {
    "亲昵动作": ("抱抱", "摸摸", "亲亲", "贴贴", "蹭蹭", "啵啵"),
    "亲密表达": ("想你", "喜欢你", "爱你", "宝贝", "宝宝", "乖乖"),
    "仪式问候": ("早安", "早上好", "晚安", "晚安安", "早呀", "睡啦"),
    "压力/疲惫": ("累", "好累", "忙", "压力", "烦", "困", "没精力"),
    "回避/不确定": ("不知道", "再说吧", "算了", "随便", "没事", "不想"),
}
TYPE_LABELS = {1: "文本", 3: "图片", 34: "语音", 47: "表情", 43: "视频"}


def _period_stats(rows: list[dict]) -> dict:
    """统计一个时间段的消息形式和可复核短语，不评价感情好坏。"""
    by_role = defaultdict(lambda: {"messages": 0, "characters": 0, "short": 0, "long": 0})
    types, phrases = Counter(), {name: Counter() for name in PHRASE_GROUPS}
    for row in rows:
        role = "我" if row["is_self"] else "对方"
        content = str(row["content"] or "")
        size = len(content.strip()) if row["local_type"] == 1 else 0
        entry = by_role[role]
        entry["messages"] += 1
        entry["characters"] += size
        entry["short"] += int(0 < size <= 12)
        entry["long"] += int(size >= 80)
        types[TYPE_LABELS.get(row["local_type"], row["type_name"] or "其他")] += 1
        for group, terms in PHRASE_GROUPS.items():
            for term in terms:
                count = content.count(term)
                if count:
                    phrases[group][term] += count
    roles = {}
    for role, item in by_role.items():
        messages = item["messages"] or 1
        roles[role] = {**item, "avg_characters": round(item["characters"] / messages, 1)}
    return {"message_count": len(rows), "by_role": roles, "types": dict(types),
            "phrases": {key: dict(value) for key, value in phrases.items()}}


def _bucket_key(timestamp: float, granularity: str) -> str:
    """将时间戳归入日、周或月桶。"""
    value = datetime.fromtimestamp(timestamp)
    if granularity == "month":
        return value.strftime("%Y-%m")
    if granularity == "week":
        year, week, _ = value.isocalendar()
        return f"{year}-W{week:02d}"
    return value.strftime("%Y-%m-%d")


def _trend_delta(recent: dict, previous: dict) -> dict:
    """返回可解释的前后期计数差，不把统计差异读成内心结论。"""
    def phrase_total(data: dict, group: str) -> int:
        return sum(data["phrases"].get(group, {}).values())
    return {
        "messages": recent["message_count"] - previous["message_count"],
        "亲昵动作": phrase_total(recent, "亲昵动作") - phrase_total(previous, "亲昵动作"),
        "亲密表达": phrase_total(recent, "亲密表达") - phrase_total(previous, "亲密表达"),
        "仪式问候": phrase_total(recent, "仪式问候") - phrase_total(previous, "仪式问候"),
        "压力/疲惫": phrase_total(recent, "压力/疲惫") - phrase_total(previous, "压力/疲惫"),
        "回避/不确定": phrase_total(recent, "回避/不确定") - phrase_total(previous, "回避/不确定"),
    }


def analyze_message_statistics(contact_name: str, days: int = 0,
                               end_time: float = 0, granularity: str = "day",
                               include_buckets: bool = False) -> dict:
    """按相邻等长时间窗统计消息变化；所有结论均回溯到原始消息。"""
    if granularity not in {"day", "week", "month"}:
        raise ValueError("granularity 必须为 day、week 或 month")
    requested_days = int(days)
    with connect() as conn:
        coverage = conn.execute("SELECT MIN(create_time),MAX(create_time) FROM messages WHERE contact_name=?", (contact_name,)).fetchone()
    earliest, latest = (coverage[0], coverage[1]) if coverage else (None, None)
    end = datetime.fromtimestamp(end_time) if end_time else (datetime.fromtimestamp(latest) if latest else datetime.now())
    available_days = max(0, int((end.timestamp() - earliest) / 86400)) if earliest else 0
    days = (min(90, max(7, available_days // 2)) if requested_days <= 0 else max(1, min(requested_days, 365)))
    recent_start, previous_start = end - timedelta(days=days), end - timedelta(days=days * 2)
    with connect() as conn:
        rows = conn.execute("""SELECT create_time,is_self,local_type,type_name,content
          FROM messages WHERE contact_name=? AND create_time>=? AND create_time<=?
          ORDER BY create_time""", (contact_name, previous_start.timestamp(), end.timestamp())).fetchall()
    records = [dict(row) for row in rows]
    recent_rows = [row for row in records if row["create_time"] >= recent_start.timestamp()]
    previous_rows = [row for row in records if row["create_time"] < recent_start.timestamp()]
    buckets = defaultdict(list)
    for row in records:
        buckets[_bucket_key(row["create_time"], granularity)].append(row)
    recent, previous = _period_stats(recent_rows), _period_stats(previous_rows)
    return {
        "contact": contact_name, "range": {"previous_start": previous_start.isoformat(timespec="seconds"),
                  "recent_start": recent_start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
                  "days_per_period": days, "granularity": granularity,
                  "archive_earliest": datetime.fromtimestamp(earliest).isoformat(timespec="seconds") if earliest else "",
                  "archive_available_days": available_days,
                  "auto_window": requested_days <= 0},
        "recent": recent, "previous": previous, "delta": _trend_delta(recent, previous),
        "buckets": ([{"period": key, **_period_stats(value)} for key, value in sorted(buckets.items())]
                    if include_buckets else []),
        "bucket_count": len(buckets),
        "disclaimer": "这是消息形式和短语的客观计数。变化需要结合事件、压力、关系阶段和原话核对，不能单独推断感情。",
    }


def recent_message_trend_context(contact_name: str) -> str:
    """生成短趋势摘要，帮助模型以近期事实覆盖过期标签。"""
    data = analyze_message_statistics(contact_name, days=14, granularity="week")
    if not data["recent"]["message_count"]:
        return ""
    delta = data["delta"]
    lines = ["## 近 14 天 vs 前 14 天的消息事实趋势（必须结合情节解释）",
             f"- 消息数变化：{delta['messages']:+d}",
             f"- 亲昵动作：{delta['亲昵动作']:+d}；亲密表达：{delta['亲密表达']:+d}；仪式问候：{delta['仪式问候']:+d}",
             f"- 压力/疲惫词：{delta['压力/疲惫']:+d}；回避/不确定词：{delta['回避/不确定']:+d}",
             "这些是计数，不是对感情的结论；若与旧记忆冲突，优先核对近期情节和原始消息。"]
    return "\n".join(lines)


def get_message_rollup(contact_name: str, level: str = "auto",
                       start_time: float = 0, end_time: float = 0,
                       limit: int = 36) -> dict:
    """返回不含原文的分层时间轴；用于先找拐点、再按更细粒度下钻。"""
    with connect() as conn:
        coverage = conn.execute("SELECT MIN(create_time),MAX(create_time) FROM messages WHERE contact_name=?", (contact_name,)).fetchone()
    if not coverage or coverage[0] is None:
        return {"contact": contact_name, "level": "month", "periods": [], "coverage_days": 0}
    start = start_time or coverage[0]
    end = end_time or coverage[1]
    coverage_days = max(0, int((end - start) / 86400))
    if level == "auto":
        level = "month" if coverage_days > 90 else "week" if coverage_days > 21 else "day"
    if level not in {"day", "week", "month"}:
        raise ValueError("level 必须为 auto、month、week 或 day")
    with connect() as conn:
        rows = conn.execute("""SELECT create_time,is_self,local_type,type_name,content FROM messages
          WHERE contact_name=? AND create_time>=? AND create_time<=? ORDER BY create_time""",
          (contact_name, start, end)).fetchall()
    buckets = defaultdict(list)
    for row in rows:
        item = dict(row)
        buckets[_bucket_key(item["create_time"], level)].append(item)
    result = []
    for period, items in sorted(buckets.items())[-max(1, min(int(limit), 60)):]:
        summary = _period_stats(items)
        result.append({"period": period, "messages": summary["message_count"],
                       "roles": {key: value["messages"] for key, value in summary["by_role"].items()},
                       "types": summary["types"],
                       "phrase_totals": {key: sum(value.values()) for key, value in summary["phrases"].items()}})
    return {"contact": contact_name, "level": level, "coverage_days": coverage_days,
            "start": datetime.fromtimestamp(start).isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(end).isoformat(timespec="seconds"), "periods": result,
            "next_step": "先根据相邻时期的显著变化选一个时间段，再用更细 level 下钻；不要读取整段原文。"}
