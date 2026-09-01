"""LearnLove 独立对话账本：追加保存、搜索、读取和 Hub 归档导入。"""

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from agent.archive import connect


def _now() -> str:
    """返回带时区、可排序的本地时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def save_conversation_entry(role: str, content: str, session_key: str = "",
                            contact_name: str = "", source: str = "learnlove",
                            source_sid: str = "", source_event_id: str = "",
                            sequence: int = 0, metadata: dict | None = None,
                            created_at: str = "") -> dict:
    """幂等追加一条完整对话记录；有来源事件 ID 时重复导入不会复制。"""
    content = "" if content is None else str(content)
    if not role or not content:
        return {"entry_id": "", "inserted": False}
    if source_event_id:
        stable = f"{source}\0{source_sid}\0{source_event_id}".encode("utf-8")
        entry_id = "conv_" + hashlib.sha256(stable).hexdigest()[:32]
    else:
        entry_id = "conv_" + uuid.uuid4().hex
    with connect() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO conversation_entries
               (entry_id,session_key,contact_name,role,content,source,source_sid,
                source_event_id,sequence,metadata_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (entry_id, session_key, contact_name, role, content, source, source_sid,
             source_event_id, int(sequence or 0),
             json.dumps(metadata or {}, ensure_ascii=False), created_at or _now()),
        )
    return {"entry_id": entry_id, "inserted": cursor.rowcount > 0}


def search_conversations(keyword: str = "", contact_name: str = "",
                         session_key: str = "", source_sid: str = "",
                         role: str = "", limit: int = 20, offset: int = 0,
                         max_content_chars: int = 600) -> list[dict]:
    """按关键词和范围搜索账本，返回可继续用 entry_id 精确读取的结果。"""
    where, params = ["1=1"], []
    if keyword:
        where.append("content LIKE ?")
        params.append(f"%{keyword}%")
    if contact_name:
        where.append("contact_name=?")
        params.append(contact_name)
    if session_key:
        where.append("session_key=?")
        params.append(session_key)
    if source_sid:
        where.append("source_sid=?")
        params.append(str(source_sid))
    if role:
        where.append("role=?")
        params.append(role)
    params.extend([
        max(1, min(int(limit or 20), 200)),
        max(0, int(offset or 0)),
    ])
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM conversation_entries WHERE {' AND '.join(where)} "
            "ORDER BY rowid DESC LIMIT ? OFFSET ?", params,
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        if len(item["content"]) > max_content_chars:
            item["content"] = item["content"][:max_content_chars] + "…"
            item["truncated"] = True
        result.append(item)
    return result


def read_conversation_entry(entry_id: str) -> dict | None:
    """按稳定 entry_id 读取一条未裁剪的完整记录。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_entries WHERE entry_id=?", (entry_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    return item


def list_conversation_sessions(limit: int = 50) -> list[dict]:
    """列出可搜索或恢复的本地会话/Hub 来源。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT session_key,source_sid,contact_name,source,COUNT(*) AS entries,
                      MIN(created_at) AS first_at,MAX(created_at) AS last_at
               FROM conversation_entries
               GROUP BY session_key,source_sid,contact_name,source
               ORDER BY MAX(rowid) DESC LIMIT ?""",
            (max(1, min(int(limit or 50), 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def restore_conversation_messages(source_sid: str = "", session_key: str = "",
                                  contact_name: str = "", limit: int = 400) -> list[dict]:
    """从账本恢复模型可继续使用的 user/assistant 消息，保持原始顺序。"""
    where, params = ["role IN ('user','assistant')"], []
    if source_sid:
        where.append("source_sid=?")
        params.append(str(source_sid))
    elif session_key:
        where.append("session_key=?")
        params.append(session_key)
    elif contact_name:
        where.append("contact_name=?")
        params.append(contact_name)
    else:
        return []
    params.append(max(1, min(int(limit or 400), 5000)))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT role,content FROM (SELECT rowid,role,content FROM conversation_entries "
            f"WHERE {' AND '.join(where)} ORDER BY rowid DESC LIMIT ?) ORDER BY rowid ASC",
            params,
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def resolve_hub_archive(source_sid: str, archive_path: str = "") -> Path:
    """解析 Hub JSONL 归档路径，支持显式文件、目录、环境变量与同级默认目录。"""
    sid = str(source_sid or "").strip()
    candidates = []
    if archive_path:
        explicit = Path(os.path.expanduser(archive_path)).resolve()
        candidates.append(explicit / f"{sid}.jsonl" if explicit.is_dir() else explicit)
    configured = os.environ.get("AGENT_HUB_ARCHIVE_DIR", "").strip()
    if configured:
        candidates.append(Path(os.path.expanduser(configured)).resolve() / f"{sid}.jsonl")
    project_parent = Path(__file__).resolve().parents[2]
    candidates.append(project_parent / "AgentHub" / "data" / "archives" / f"{sid}.jsonl")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"找不到 Hub 归档 {sid!r}；请提供 archive_path 或设置 AGENT_HUB_ARCHIVE_DIR"
    )


def import_hub_archive(source_sid: str, archive_path: str = "",
                       contact_name: str = "") -> dict:
    """导入 Hub 展示事件，重建用户/助手对话并保存可检索的工具和系统记录。"""
    path = resolve_hub_archive(source_sid, archive_path)
    sid = str(source_sid or path.stem)
    session_key = f"hub-archive:{sid}"
    inserted = 0
    seen = 0
    assistant_buffer = []

    def save(role: str, content: str, index: int, event_type: str, metadata=None):
        """按归档行号稳定去重写入。"""
        nonlocal inserted
        result = save_conversation_entry(
            role=role, content=content, session_key=session_key,
            contact_name=contact_name, source="hub_archive", source_sid=sid,
            source_event_id=f"{index}:{event_type}", sequence=index,
            metadata={"event_type": event_type, "archive_path": str(path), **(metadata or {})},
        )
        inserted += int(result["inserted"])

    def flush_buffer(index: int, event_type: str):
        """没有 assistant_final 时，用流式增量拼成完整助手消息。"""
        nonlocal assistant_buffer
        if assistant_buffer:
            save("assistant", "".join(assistant_buffer), index, event_type)
            assistant_buffer = []

    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen += 1
            event_type = str(event.get("type") or "")
            if event_type == "user":
                flush_buffer(index, "assistant_recovered")
                save("user", event.get("text") or "", index, event_type,
                     {"turn": event.get("turn")})
            elif event_type == "assistant_delta":
                assistant_buffer.append(str(event.get("content") or ""))
            elif event_type == "assistant_final":
                assistant_buffer = []
                save("assistant", event.get("content") or "", index, event_type)
            elif event_type == "assistant_end":
                flush_buffer(index, event_type)
            elif event_type == "log" and event.get("text"):
                save("system", event["text"], index, event_type,
                     {"level": event.get("level")})
            elif event_type == "tool_start":
                save("tool", json.dumps({"name": event.get("name"), "args": event.get("args")},
                                         ensure_ascii=False, default=str),
                     index, event_type, {"tool_id": event.get("id")})
            elif event_type == "tool_end":
                save("tool", json.dumps({"name": event.get("name"), "ok": event.get("ok"),
                                          "error": event.get("error")},
                                         ensure_ascii=False, default=str),
                     index, event_type, {"tool_id": event.get("id")})
    flush_buffer(seen + 1, "assistant_recovered_eof")
    restored = restore_conversation_messages(session_key=session_key, limit=5000)
    return {
        "source_sid": sid,
        "archive_path": str(path),
        "events_seen": seen,
        "entries_inserted": inserted,
        "conversation_messages": len(restored),
        "session_key": session_key,
    }
