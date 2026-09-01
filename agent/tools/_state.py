"""
共享状态单例 — 所有 agent 工具共享的全局状态
"""

import os
import sys
import json
import queue
import threading
import tempfile


class DBCache:
    """mtime-based 解密数据库缓存。

    与 live_monitor.py / mcp_server.py 的 DBCache 完全一致。
    get(rel_key) → 返回解密后的临时 .db 文件路径。
    """

    def __init__(self, db_dir: str, all_keys: dict):
        self.db_dir = db_dir
        self.all_keys = all_keys
        self._cache = {}  # rel_key -> (db_mtime, wal_mtime, tmp_path)
        self._lock = threading.Lock()

    def get(self, rel_key: str) -> str | None:
        """获取解密后的 SQLite 临时文件路径。mtime 未变则复用缓存。"""
        from agent.tools._decrypt import full_decrypt, decrypt_wal

        if rel_key not in self.all_keys:
            return None

        rel_path = rel_key.replace("\\", os.sep)
        db_path = os.path.join(self.db_dir, rel_path)
        wal_path = db_path + "-wal"

        if not os.path.exists(db_path):
            return None

        try:
            db_mtime = os.path.getmtime(db_path)
            wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        except OSError:
            return None

        with self._lock:
            if rel_key in self._cache:
                c_db_mt, c_wal_mt, c_path = self._cache[rel_key]
                if c_db_mt == db_mtime and c_wal_mt == wal_mtime and os.path.exists(c_path):
                    return c_path
                # 过期，清理
                try:
                    os.unlink(c_path)
                except OSError:
                    pass

        enc_key = bytes.fromhex(self.all_keys[rel_key]["enc_key"])
        fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="llagent_")
        os.close(fd)

        full_decrypt(db_path, tmp_path, enc_key)
        if os.path.exists(wal_path):
            decrypt_wal(wal_path, tmp_path, enc_key)

        with self._lock:
            self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)

        return tmp_path

    def get_msg_db_keys(self) -> list[str]:
        """返回所有消息数据库的 rel_key（不含 fts/resource/biz）"""
        return sorted([
            k for k in self.all_keys
            if k.startswith("message\\message_") and k.endswith(".db")
            and "fts" not in k and "resource" not in k and "biz" not in k
        ])

    def get_media_db_path(self) -> str | None:
        """返回解密后的 media_0.db 路径"""
        media_key = "message\\media_0.db"
        return self.get(media_key)

    def cleanup(self):
        """清理所有缓存临时文件"""
        with self._lock:
            for _, (_, _, tmp_path) in self._cache.items():
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            self._cache.clear()


class AgentState:
    """全局共享状态单例"""

    def __init__(self):
        from agent.paths import (
            live_feed_path, media_preferences_path, monitor_state_path, voice_cache_dir,
        )

        self.db_cache: DBCache | None = None
        self.contacts: dict = {}           # {wxid: {nick, remark, display, alias}}
        self.sender_maps: dict = {}         # {(wxid, table_name): {sender_id: name}}
        self.monitor_queue: queue.Queue = queue.Queue()
        self.monitor_thread: threading.Thread | None = None
        self.monitor_running = False
        self.live_feed_path: str = live_feed_path()
        self.monitor_state_path: str = monitor_state_path()
        self.voice_cache_dir: str = voice_cache_dir()
        self.media_preferences_path: str = media_preferences_path()
        self.active_contact_wxid: str | None = None
        self.active_contact_name: str = ""

        # 配置引用（由 loop.py 设置）
        self.config: dict = {}

    def media_mode(self, kind: str) -> str:
        """读取全局媒体模式；默认手动，避免未授权的重型自动推理。"""
        key = f"{kind}_mode"
        configured = (self.config.get("media", {}) or {}).get(key, "manual")
        return "auto" if configured in ("auto", "api") else "manual"

    def set_media_mode(self, kind: str, mode: str) -> str:
        """更新并持久化全局语音或识图模式。"""
        if kind not in ("voice", "image"):
            raise ValueError("媒体类型必须是 voice 或 image")
        normalized = "auto" if mode in ("auto", "api") else "manual"
        if mode not in ("auto", "api", "manual"):
            raise ValueError("模式必须是 auto 或 manual")
        self.config.setdefault("media", {})[f"{kind}_mode"] = normalized
        os.makedirs(os.path.dirname(self.media_preferences_path), exist_ok=True)
        with open(self.media_preferences_path, "w", encoding="utf-8") as file:
            json.dump(self.config["media"], file, ensure_ascii=False, indent=2)
        return normalized

    def load_media_preferences(self) -> None:
        """加载命令写入的本机媒体模式，覆盖配置中的默认值。"""
        if not os.path.exists(self.media_preferences_path):
            return
        try:
            saved = json.load(open(self.media_preferences_path, encoding="utf-8"))
            if isinstance(saved, dict):
                self.config.setdefault("media", {}).update(saved)
        except (OSError, json.JSONDecodeError):
            pass

    def setup(self, db_dir: str, keys_file: str):
        """初始化：加载密钥、创建 DBCache"""
        with open(keys_file) as f:
            all_keys = json.load(f)
        self.db_cache = DBCache(db_dir, all_keys)

    def load_contacts(self):
        """从解密后的 contact.db 加载所有联系人"""
        contact_key = "contact\\contact.db"
        contact_path = self.db_cache.get(contact_key)
        if not contact_path:
            raise RuntimeError("无法加载联系人数据库")

        import sqlite3
        conn = sqlite3.connect(contact_path)
        for uname, nick, remark, alias in conn.execute(
            "SELECT username, nick_name, remark, alias FROM contact"
        ).fetchall():
            display = remark or nick or uname
            self.contacts[uname] = {
                "nick": nick or "",
                "remark": remark or "",
                "display": display,
                "alias": alias or "",
            }
        conn.close()

    def resolve_contact(self, query: str) -> tuple[str | None, str | None]:
        """模糊搜索联系人 → (wxid, display_name)"""
        q = query.lower()
        for wxid, info in self.contacts.items():
            candidates = [
                (info["display"] or "").lower(),
                (info["nick"] or "").lower(),
                (info["remark"] or "").lower(),
                (info["alias"] or "").lower(),
                wxid.lower(),
            ]
            if any(q in c for c in candidates if c):
                return wxid, info["display"]
        return None, None

    def resolve_contact_exact(self, query: str) -> tuple[str | None, str | None]:
        """精确优先匹配联系人"""
        for wxid, info in self.contacts.items():
            if info["display"] == query or info["remark"] == query or info["nick"] == query:
                return wxid, info["display"]
        return self.resolve_contact(query)

    def contacts_config(self) -> list[dict]:
        """返回 config.yaml 中配置的监听联系人列表"""
        return self.config.get("contacts", [])

    def contact_config(self, wxid: str) -> dict | None:
        """获取某个联系人的配置"""
        for c in self.contacts_config():
            if c.get("wxid") == wxid:
                return c
        return None

    def voice_mode(self, wxid: str) -> str:
        """兼容旧调用：语音处理已改为服务用户的全局模式。"""
        return self.media_mode("voice")

    def whisper_model_name(self, wxid: str) -> str:
        """已废弃：本地 Whisper 已停用，仅为旧扩展保留兼容返回值。"""
        return ""

    def load_state(self) -> dict:
        """加载监控状态"""
        if os.path.exists(self.monitor_state_path):
            with open(self.monitor_state_path) as f:
                return json.load(f)
        return {}

    def save_state(self, s: dict):
        """保存监控状态"""
        os.makedirs(os.path.dirname(self.monitor_state_path), exist_ok=True)
        with open(self.monitor_state_path, "w") as f:
            json.dump(s, f)


# 全局单例
state = AgentState()
