"""把规范化微信消息整理成面向 Agent 的稳定时间线。"""

from collections import Counter


def _clean_text(value: object, limit: int = 2000) -> str:
    """清理展示文本，同时保留足够的事实正文。"""
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _agent_record(message: dict) -> dict:
    """从完整事实对象提取 Agent 推理需要的最小字段。"""
    role = message.get("sender_role")
    if role not in {"self", "contact", "system", "unknown"}:
        role = "self" if message.get("is_self") else (
            "unknown" if str(message.get("sender", "")).startswith(("sid=", "未知发送者"))
            else "contact"
        )
    media = message.get("media") if isinstance(message.get("media"), dict) else {}
    quote = message.get("quote") if isinstance(message.get("quote"), dict) else {}
    record = {
        "id": message.get("message_id", ""),
        "time": message.get("time", ""),
        "timestamp": message.get("create_time", 0),
        "speaker_role": role,
        "speaker": "我" if role == "self" else message.get("sender", "未知发送者"),
        "kind": message.get("type", "未知"),
        "text": _clean_text(message.get("content")),
    }
    if quote:
        record["quote"] = {
            "speaker": _clean_text(quote.get("sender"), 100) or "未知",
            "text": _clean_text(quote.get("content")),
            "type": quote.get("type", 0),
            "source_id": quote.get("server_id", ""),
        }
    if media:
        record["media"] = {
            "kind": media.get("kind", ""),
            "status": media.get("status", "pending"),
            "result": _clean_text(media.get("result")),
            "error": _clean_text(media.get("error"), 300),
        }
    return record


def build_agent_timeline(messages: list[dict], contact_name: str = "") -> dict:
    """生成按时间正序、角色明确、引用与媒体分层的 Agent 时间线契约。"""
    ordered = sorted(
        messages,
        key=lambda message: (
            message.get("create_time", 0),
            message.get("sort_seq", 0),
            message.get("server_seq", 0),
            message.get("local_id", 0),
            message.get("message_id", ""),
        ),
    )
    records = [_agent_record(message) for message in ordered]
    role_counts = Counter(record["speaker_role"] for record in records)
    kind_counts = Counter(record["kind"] for record in records)
    warnings = []
    unknown = role_counts.get("unknown", 0)
    if unknown:
        warnings.append(f"{unknown} 条消息的发送者无法由数据库事实确认，未猜测归属")
    pending = sum(
        1 for record in records
        if record.get("media", {}).get("status") in {"pending", "archived", "missing"}
    )
    if pending:
        warnings.append(f"{pending} 条媒体消息尚未完成识别，正文保留占位符")

    lines = [
        "# 微信聊天时间线",
        f"联系人：{contact_name or '未指定'}",
        "规则：严格按时间升序；“我”指当前微信账号；未知发送者不会被当作任何一方。",
    ]
    for record in records:
        header = (
            f"[{record['time'] or record['timestamp']}]"
            f"[{record['speaker']}][{record['kind']}][id={record['id']}]"
        )
        lines.append(f"{header} {record['text']}")
        quote = record.get("quote")
        if quote:
            source = f" id={quote['source_id']}" if quote.get("source_id") else ""
            lines.append(
                f"  ↳ 引用 {quote['speaker']}（类型 {quote['type']}{source}）：{quote['text']}"
            )
        media = record.get("media")
        if media:
            detail = media.get("result") or media.get("error") or "等待处理"
            lines.append(f"  ↳ 媒体识别 {media['status']}：{detail}")

    return {
        "format": "learnlove.chat.timeline.v1",
        "ordering": "chronological",
        "speaker_legend": {
            "self": "当前微信账号（我）",
            "contact": contact_name or "当前联系人",
            "system": "微信客户端或服务消息",
            "unknown": "数据库映射不足，禁止猜测",
        },
        "records": records,
        "transcript": "\n".join(lines),
        "diagnostics": {
            "message_count": len(records),
            "speaker_counts": dict(role_counts),
            "type_counts": dict(kind_counts),
            "warnings": warnings,
        },
    }
