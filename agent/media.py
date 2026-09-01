"""微信语音与图片文件归档。识别由 media_api 模块异步完成。"""

import hashlib
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from agent.archive import upsert_media
from agent.paths import archived_media_dir
from agent.tools._state import state


def _write_bytes(kind: str, contact_wxid: str, create_time: float,
                 data: bytes, extension: str) -> tuple[str,str]:
    """按内容哈希保存字节，重复归档不会产生副本。"""
    digest = hashlib.sha256(data).hexdigest()
    month = datetime.fromtimestamp(create_time).strftime("%Y-%m")
    target_dir = Path(archived_media_dir()) / kind / contact_wxid / month
    target_dir.mkdir(parents=True,exist_ok=True)
    target = target_dir / f"{digest}{extension}"
    if not target.exists():
        target.write_bytes(data)
    return str(target),digest


def archive_voice(message: dict) -> dict:
    """从 media_0.db 提取原始语音并加入语音处理队列。"""
    media_path = state.db_cache.get_media_db_path() if state.db_cache else None
    if not media_path:
        return {"status":"missing","error":"无法访问 media_0.db"}
    conn = sqlite3.connect(media_path)
    try:
        row = conn.execute("""SELECT voice_data,data_index FROM VoiceInfo
            WHERE create_time=? AND voice_data IS NOT NULL
            ORDER BY data_index LIMIT 1""",(message["create_time"],)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"status":"missing","error":"语音原始数据尚未落库"}
    path,digest = _write_bytes("voice",message["contact_wxid"],
                               message["create_time"],row[0],".silk")
    return upsert_media({"message_id":message["message_id"],
      "contact_wxid":message["contact_wxid"],"kind":"voice",
      "archived_path":path,"mime_type":"audio/silk","sha256":digest,
      "metadata":{**(message.get("metadata") or {}),"data_index":row[1]}},
      "speech_to_text")


def _image_roots() -> list[Path]:
    """推导微信附件目录，兼容 db_storage 位于微信数据根目录内的布局。"""
    if not state.db_cache:
        return []
    db_dir = Path(state.db_cache.db_dir)
    bases = [db_dir.parent,db_dir]
    roots = []
    for base in bases:
        for rel in ("msg/attach","Msg/Attach","attach","Attach"):
            candidate = base / rel
            if candidate.exists() and candidate not in roots:
                roots.append(candidate)
    return roots


def archive_image(message: dict) -> dict:
    """查找并归档图片原文件；加密 .dat 保留原样，等待后续解码/API。"""
    meta = message.get("metadata") or {}
    md5 = (meta.get("md5") or meta.get("file_md5") or "").lower()
    candidates = []
    for root in _image_roots():
        patterns = [f"{md5}*"] if md5 else []
        for pattern in patterns:
            candidates.extend(path for path in root.rglob(pattern) if path.is_file())
            if candidates:
                break
    if not candidates:
        return upsert_media({"message_id":message["message_id"],
          "contact_wxid":message["contact_wxid"],"kind":"image",
          "metadata":{**meta,"archive_error":"未定位到图片文件"}},
          "image_understanding")
    source = max(candidates,key=lambda path:path.stat().st_size)
    data = source.read_bytes()
    ext = source.suffix.lower() or ".dat"
    path,digest = _write_bytes("image",message["contact_wxid"],
                               message["create_time"],data,ext)
    mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
            "gif":"image/gif"}.get(ext.lstrip("."),"application/octet-stream")
    return upsert_media({"message_id":message["message_id"],
      "contact_wxid":message["contact_wxid"],"kind":"image",
      "source_path":str(source),"archived_path":path,"mime_type":mime,
      "sha256":digest,"metadata":{**meta,"encrypted":ext==".dat"}},
      "image_understanding")


def archive_message_media(message: dict) -> dict | None:
    """根据规范化消息类型归档媒体，普通消息不处理。"""
    if message.get("local_type") == 34:
        return archive_voice(message)
    if message.get("local_type") == 3:
        return archive_image(message)
    return None
