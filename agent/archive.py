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
CREATE TABLE IF NOT EXISTS relationship_signals (
 signal_id TEXT PRIMARY KEY, contact_name TEXT NOT NULL, observed_at TEXT NOT NULL,
 dimension TEXT NOT NULL, direction INTEGER NOT NULL, confidence TEXT NOT NULL,
 evidence_json TEXT DEFAULT '[]', alternatives_json TEXT DEFAULT '[]',
 trigger_text TEXT DEFAULT '', recommended_action TEXT DEFAULT '',
 source_message_ids_json TEXT DEFAULT '[]', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_relationship_signals_contact_time
 ON relationship_signals(contact_name,observed_at DESC);
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


RELATIONSHIP_DIMENSIONS = {
    "mutuality": "互动互惠",
    "emotional_safety": "情绪安全",
    "repair_willingness": "修复意愿",
    "closeness_investment": "亲近/投入",
    "communication_pressure": "沟通压力",
}


def record_relationship_signal(contact_name: str, dimension: str, direction: int,
                               evidence=None, alternatives=None, trigger_text: str = "",
                               recommended_action: str = "", confidence: str = "low",
                               observed_at: str = "", source_message_ids=None) -> dict:
    """记录一条可复核的关系信号；方向是变化，不是对对方感情的断言。"""
    if dimension not in RELATIONSHIP_DIMENSIONS:
        raise ValueError("dimension 必须是: " + ", ".join(RELATIONSHIP_DIMENSIONS))
    direction = int(direction)
    if direction not in (-2, -1, 0, 1, 2):
        raise ValueError("direction 必须为 -2、-1、0、1、2")
    if confidence not in ("low", "medium", "high"):
        raise ValueError("confidence 必须为 low、medium 或 high")
    now = _now()
    data = (f"sig_{uuid.uuid4().hex}", contact_name or "自己", observed_at or now,
            dimension, direction, confidence, json.dumps(evidence or [], ensure_ascii=False),
            json.dumps(alternatives or [], ensure_ascii=False), trigger_text.strip(),
            recommended_action.strip(), json.dumps(source_message_ids or [], ensure_ascii=False), now)
    with connect() as conn:
        conn.execute("""INSERT INTO relationship_signals
          (signal_id,contact_name,observed_at,dimension,direction,confidence,evidence_json,
           alternatives_json,trigger_text,recommended_action,source_message_ids_json,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", data)
    return {"signal_id": data[0], "dimension": dimension, "direction": direction}


def relationship_dashboard(contact_name: str, limit: int = 30) -> dict:
    """汇总近期信号为趋势仪表盘；结果仅供复核，不生成单一好感度。"""
    with connect() as conn:
        rows = conn.execute("""SELECT * FROM relationship_signals WHERE contact_name=?
          ORDER BY observed_at DESC,created_at DESC LIMIT ?""",
          (contact_name, max(1, min(int(limit), 100)))).fetchall()
    signals = []
    grouped = {key: [] for key in RELATIONSHIP_DIMENSIONS}
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        item["alternatives"] = json.loads(item.pop("alternatives_json") or "[]")
        item["source_message_ids"] = json.loads(item.pop("source_message_ids_json") or "[]")
        signals.append(item)
        grouped[item["dimension"]].append(item)
    dimensions = {}
    for key, items in grouped.items():
        recent = list(reversed(items[:5]))
        if not recent:
            dimensions[key] = {"label": RELATIONSHIP_DIMENSIONS[key], "state": "信号不足", "count": 0}
            continue
        average = sum(item["direction"] for item in recent) / len(recent)
        if key == "communication_pressure":
            state = "压力上升" if average >= 0.75 else "压力下降" if average <= -0.75 else "波动/待观察"
        else:
            state = "上升" if average >= 0.75 else "下降" if average <= -0.75 else "波动/待观察"
        dimensions[key] = {"label": RELATIONSHIP_DIMENSIONS[key], "state": state,
                           "average": round(average, 2), "count": len(recent),
                           "latest": recent[-1]}
    alerts = []
    pressure = dimensions["communication_pressure"]
    investment = dimensions["closeness_investment"]
    repair = dimensions["repair_willingness"]
    if pressure.get("average", 0) >= 0.75 and (investment.get("average", 0) <= -0.75 or repair.get("average", 0) <= -0.75):
        alerts.append("预警：压力上升且投入或修复意愿下降；应结合触发事件安排一次真诚、非逼迫的澄清。")
    elif any(value.get("average", 0) <= -0.75 for key, value in dimensions.items() if key != "communication_pressure"):
        alerts.append("提醒：出现持续下降信号；先核对人物处境、事件链与其他解释，不要急于给关系下结论。")
    return {"contact": contact_name, "dimensions": dimensions, "alerts": alerts,
            "signals": signals[:10], "disclaimer": "趋势来自已记录信号，不是好感度或读心结论。"}


def recent_relationship_signals_context(contact_name: str) -> str:
    """生成精简趋势上下文，提醒模型先核对证据再提出行动。"""
    dashboard = relationship_dashboard(contact_name, limit=12)
    if not dashboard["signals"]:
        return ""
    lines = ["## 关系信号趋势（待核实，不是结论）"]
    for value in dashboard["dimensions"].values():
        if value.get("count"):
            lines.append(f"- {value['label']}：{value['state']}（{value['count']} 条已记录信号）")
    for alert in dashboard["alerts"]:
        lines.append(f"- {alert}")
    lines.append("使用前先检查本次情节是否改变了旧判断，不得把趋势当作对方内心事实。")
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


def pending_media_jobs(capability: str = "", limit: int = 20,
                       include_failed: bool = True) -> list[dict]:
    """返回待处理任务；自动处理不重复执行已失败任务，手动补跑才会重试。"""
    statuses = "('pending','failed')" if include_failed else "('pending')"
    where, params = f"WHERE j.status IN {statuses}", []
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
