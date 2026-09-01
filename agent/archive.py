"""统一事实归档：消息、事件、媒体和待处理 API 任务。"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from agent.paths import archived_media_dir, records_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, contact_wxid TEXT NOT NULL, contact_name TEXT NOT NULL,
 create_time REAL NOT NULL, sort_seq INTEGER DEFAULT 0, server_seq INTEGER DEFAULT 0,
 local_id INTEGER DEFAULT 0, server_id INTEGER DEFAULT 0, sender_id INTEGER,
 sender_wxid TEXT DEFAULT '', sender_name TEXT NOT NULL, is_self INTEGER DEFAULT 0,
 local_type INTEGER NOT NULL, type_name TEXT NOT NULL, content TEXT DEFAULT '',
 raw_content TEXT DEFAULT '', quote_json TEXT DEFAULT '{}', metadata_json TEXT DEFAULT '{}',
 source_db TEXT DEFAULT '', source_table TEXT DEFAULT '', archived_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_contact_time ON messages(contact_wxid,create_time,sort_seq,local_id);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, contact_name TEXT NOT NULL, title TEXT NOT NULL,
 summary TEXT NOT NULL, narrative TEXT NOT NULL, event_time TEXT DEFAULT '',
 participants_json TEXT DEFAULT '[]', facts_json TEXT DEFAULT '[]',
 emotions_json TEXT DEFAULT '[]', uncertainties_json TEXT DEFAULT '[]',
 tags_json TEXT DEFAULT '[]', source_message_ids_json TEXT DEFAULT '[]',
 status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_contact_updated ON events(contact_name,updated_at DESC);
CREATE TABLE IF NOT EXISTS event_revisions (
 revision_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, revised_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media (
 media_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, contact_wxid TEXT NOT NULL,
 kind TEXT NOT NULL, source_path TEXT DEFAULT '', archived_path TEXT DEFAULT '',
 mime_type TEXT DEFAULT '', sha256 TEXT DEFAULT '', status TEXT DEFAULT 'archived',
 api_result TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_message_kind ON media(message_id,kind);
CREATE TABLE IF NOT EXISTS media_jobs (
 job_id TEXT PRIMARY KEY, media_id TEXT NOT NULL, capability TEXT NOT NULL,
 provider TEXT DEFAULT '', status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
 last_error TEXT DEFAULT '', result_text TEXT DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_jobs_status ON media_jobs(status,created_at);
CREATE TABLE IF NOT EXISTS conversation_entries (
 entry_id TEXT PRIMARY KEY, session_key TEXT DEFAULT '', contact_name TEXT DEFAULT '',
 role TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'learnlove',
 source_sid TEXT DEFAULT '', source_event_id TEXT DEFAULT '', sequence INTEGER DEFAULT 0,
 metadata_json TEXT DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_conversation_session ON conversation_entries(session_key,sequence,created_at);
CREATE INDEX IF NOT EXISTS idx_conversation_contact ON conversation_entries(contact_name,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_source_event
 ON conversation_entries(source,source_sid,source_event_id) WHERE source_event_id != '';
"""


def _now() -> str:
    """返回可排序的本地时间字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def connect():
    """打开归档数据库并保证结构已初始化。"""
    path = records_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(archived_media_dir(), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_messages(messages: list[dict]) -> int:
    """按稳定消息 ID 幂等写入规范化消息。"""
    if not messages:
        return 0
    cols = ["message_id","contact_wxid","contact_name","create_time","sort_seq",
            "server_seq","local_id","server_id","sender_id","sender_wxid","sender_name",
            "is_self","local_type","type_name","content","raw_content","quote_json",
            "metadata_json","source_db","source_table","archived_at"]
    sql = (f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
           + "ON CONFLICT(message_id) DO UPDATE SET "
           + ",".join(f"{c}=excluded.{c}" for c in cols[1:-1]))
    now, rows = _now(), []
    for m in messages:
        rows.append((m["message_id"],m["contact_wxid"],m["contact_name"],m["create_time"],
          m.get("sort_seq",0),m.get("server_seq",0),m.get("local_id",0),m.get("server_id",0),
          m.get("sender_id"),m.get("sender_wxid",""),m.get("sender","未知"),
          int(bool(m.get("is_self"))),m["local_type"],m["type"],m.get("content",""),
          m.get("raw_content",""),json.dumps(m.get("quote") or {},ensure_ascii=False),
          json.dumps(m.get("metadata") or {},ensure_ascii=False),m.get("source_db",""),
          m.get("source_table",""),now))
    with connect() as conn:
        conn.executemany(sql,rows)
    return len(rows)


def record_event(contact_name: str, title: str, summary: str, narrative: str,
                 event_time: str = "", participants=None, facts=None, emotions=None,
                 uncertainties=None, tags=None, source_message_ids=None,
                 event_id: str = "") -> dict:
    """新增或修订事件；修订前保存完整快照，避免覆盖历史全貌。"""
    event_id = event_id.strip() or f"evt_{uuid.uuid4().hex}"
    now = _now()
    data = {"event_id":event_id,"contact_name":contact_name or "自己",
      "title":title.strip() or summary.strip()[:40] or "未命名事件",
      "summary":summary.strip(),"narrative":narrative.strip() or summary.strip(),
      "event_time":event_time.strip(),
      "participants_json":json.dumps(participants or [],ensure_ascii=False),
      "facts_json":json.dumps(facts or [],ensure_ascii=False),
      "emotions_json":json.dumps(emotions or [],ensure_ascii=False),
      "uncertainties_json":json.dumps(uncertainties or [],ensure_ascii=False),
      "tags_json":json.dumps(tags or [],ensure_ascii=False),
      "source_message_ids_json":json.dumps(source_message_ids or [],ensure_ascii=False),
      "status":"active","created_at":now,"updated_at":now}
    with connect() as conn:
        old = conn.execute("SELECT * FROM events WHERE event_id=?",(event_id,)).fetchone()
        if old:
            conn.execute("INSERT INTO event_revisions(event_id,snapshot_json,revised_at) VALUES(?,?,?)",
                         (event_id,json.dumps(dict(old),ensure_ascii=False),now))
            data["created_at"] = old["created_at"]
        cols = list(data)
        conn.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
          +"ON CONFLICT(event_id) DO UPDATE SET "+",".join(f"{c}=excluded.{c}" for c in cols[1:]),
          [data[c] for c in cols])
    return {"event_id":event_id,"updated":bool(old),"title":data["title"]}


def list_events(contact_name: str = "", keyword: str = "", limit: int = 10) -> list[dict]:
    """查询事件并还原 JSON 字段。"""
    where, params = ["status='active'"], []
    if contact_name:
        where.append("contact_name=?")
        params.append(contact_name)
    if keyword:
        where.append("(title LIKE ? OR summary LIKE ? OR narrative LIKE ?)")
        params.extend([f"%{keyword}%"]*3)
    params.append(max(1,min(int(limit),100)))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM events WHERE {' AND '.join(where)} "
                            "ORDER BY updated_at DESC LIMIT ?",params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for name in ("participants","facts","emotions","uncertainties","tags","source_message_ids"):
            item[name] = json.loads(item.pop(f"{name}_json") or "[]")
        result.append(item)
    return result


def recent_events_context(contact_name: str, limit: int = 5) -> str:
    """生成供后续分析使用的简短事件事实上下文。"""
    events = list_events(contact_name=contact_name,limit=limit)
    if not events:
        return ""
    lines = [f"## 已留存事件/故事（关于 {contact_name}）"]
    for item in reversed(events):
        when = item.get("event_time") or item.get("created_at","")[:10]
        lines.append(f"- [{item['event_id']}] {when}｜{item['title']}：{item['summary']}")
        if item.get("uncertainties"):
            lines.append("  未确认："+"；".join(str(x) for x in item["uncertainties"][:3]))
    return "\n".join(lines)


def upsert_media(item: dict, capability: str) -> dict:
    """保存媒体记录，并为本地/API 处理器创建幂等待处理任务。"""
    now = _now()
    media_id = item.get("media_id") or f"med_{item['message_id'].replace(':','_')}_{item['kind']}"
    job_id = f"job_{media_id}_{capability}"
    with connect() as conn:
        conn.execute("""INSERT INTO media(media_id,message_id,contact_wxid,kind,source_path,
          archived_path,mime_type,sha256,status,metadata_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id,kind) DO UPDATE SET
          source_path=excluded.source_path,archived_path=excluded.archived_path,
          mime_type=excluded.mime_type,sha256=excluded.sha256,
          metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
          (media_id,item["message_id"],item["contact_wxid"],item["kind"],
           item.get("source_path",""),item.get("archived_path",""),item.get("mime_type",""),
           item.get("sha256",""),"archived",json.dumps(item.get("metadata") or {},ensure_ascii=False),
           now,now))
        conn.execute("""INSERT OR IGNORE INTO media_jobs
          (job_id,media_id,capability,status,created_at,updated_at)
          VALUES(?,?,?,'pending',?,?)""",(job_id,media_id,capability,now,now))
    return {"media_id":media_id,"job_id":job_id,"status":"pending"}


def pending_media_jobs(capability: str = "", limit: int = 20) -> list[dict]:
    """返回等待语音或识图处理器执行的任务。"""
    where, params = "WHERE j.status IN ('pending','failed')", []
    if capability:
        where += " AND j.capability=?"
        params.append(capability)
    params.append(max(1,min(int(limit),100)))
    with connect() as conn:
        rows = conn.execute(f"""SELECT j.*,m.archived_path,m.mime_type,m.message_id,
          m.contact_wxid,m.kind FROM media_jobs j JOIN media m ON m.media_id=j.media_id
          {where} ORDER BY j.created_at LIMIT ?""",params).fetchall()
    return [dict(row) for row in rows]


def media_results_for_messages(message_ids: list[str]) -> dict[str,dict]:
    """读取消息对应的媒体状态与识别结果，供 Agent 时间线展示。"""
    ids = list(dict.fromkeys(str(item) for item in message_ids if item))[:500]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(f"""SELECT m.message_id,m.kind,m.status AS media_status,
          m.api_result,m.metadata_json,j.status AS job_status,j.last_error,j.result_text
          FROM media m LEFT JOIN media_jobs j ON j.media_id=m.media_id
          WHERE m.message_id IN ({placeholders})""", ids).fetchall()
    result = {}
    for row in rows:
        item = dict(row)
        metadata = json.loads(item.get("metadata_json") or "{}")
        result[item["message_id"]] = {
            "kind": item.get("kind", ""),
            "status": item.get("job_status") or item.get("media_status") or "pending",
            "result": item.get("result_text") or item.get("api_result") or "",
            "error": item.get("last_error") or metadata.get("archive_error", ""),
        }
    return result


def finish_media_job(job_id: str, provider: str, result_text: str = "", error: str = ""):
    """保存 API 结果；失败任务保持可重试。"""
    now, status = _now(), "failed" if error else "completed"
    with connect() as conn:
        row = conn.execute("SELECT media_id FROM media_jobs WHERE job_id=?",(job_id,)).fetchone()
        if not row:
            raise ValueError(f"未知媒体任务: {job_id}")
        conn.execute("""UPDATE media_jobs SET provider=?,status=?,attempts=attempts+1,
          last_error=?,result_text=?,updated_at=? WHERE job_id=?""",
          (provider,status,error,result_text,now,job_id))
        conn.execute("UPDATE media SET status=?,api_result=?,updated_at=? WHERE media_id=?",
                     (status,result_text,now,row["media_id"]))
