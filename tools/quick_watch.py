"""快速监听 — 无 Whisper，秒启动，检测新消息并写入文件"""
import os, sys, json, sqlite3, hashlib, time

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECRYPTED_DIR = os.path.join(SCRIPT_DIR, "wechat-decrypt", "decrypted")

import zstandard as zstd
dctx = zstd.ZstdDecompressor()

# 扫所有 wxid_ 开头的会话
contact_db = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
conn = sqlite3.connect(contact_db)
contacts = {}
for uname, nick, remark, alias in conn.execute(
    "SELECT username, nick_name, remark, alias FROM contact"
).fetchall():
    display = remark or nick or uname
    contacts[uname] = display
conn.close()

# 找到所有个人会话
sessions = {}
msg_dir = os.path.join(DECRYPTED_DIR, "message")
for f in sorted(os.listdir(msg_dir)):
    if not f.startswith("message_") or "fts" in f or "resource" in f or "biz" in f:
        continue
    path = os.path.join(msg_dir, f)
    c = sqlite3.connect(path)
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()
    for (tname,) in tables:
        # 反查 username
        for uname, display in contacts.items():
            h = hashlib.md5(uname.encode()).hexdigest()
            if tname == f"Msg_{h}":
                latest = c.execute(f"SELECT MAX(create_time) FROM [{tname}]").fetchone()[0]
                sessions[uname] = {
                    "display": display,
                    "table": tname,
                    "db": path,
                    "last_ts": latest or 0,
                }
    c.close()

# 仅输出有会话的
print(f"监听 {len(sessions)} 个个人会话...")
for uname, info in list(sessions.items())[:10]:
    safe = info["display"].encode('ascii', errors='replace').decode('ascii')
    print(f"  {uname[:30]}... -> {safe}")

# 主循环
FEED_FILE = os.path.join(SCRIPT_DIR, "data", "live", "incoming.jsonl")
os.makedirs(os.path.dirname(FEED_FILE), exist_ok=True)

print(f"\n输出: {FEED_FILE}")
print("现在发消息试试! Ctrl+C 停止\n")

try:
    while True:
        for uname, info in sessions.items():
            c = sqlite3.connect(info["db"])
            try:
                rows = c.execute(
                    f"SELECT create_time, local_type, message_content, "
                    f"WCDB_CT_message_content, real_sender_id "
                    f"FROM [{info['table']}] WHERE create_time > ? "
                    f"ORDER BY create_time ASC",
                    (info["last_ts"],)
                ).fetchall()
            except:
                c.close()
                continue
            c.close()

            for ts, lt, content, ct, sid in rows:
                # 解码
                if ct and ct == 4 and isinstance(content, bytes):
                    text = dctx.decompress(content).decode('utf-8', errors='replace')
                elif isinstance(content, bytes):
                    text = content.decode('utf-8', errors='replace')
                else:
                    text = str(content) if content else ""

                from datetime import datetime
                ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                sender = info["display"] if sid == 1 else "你"
                type_names = {1: "文本", 3: "图片", 34: "语音", 47: "表情",
                              48: "位置", 49: "文件", 50: "通话"}

                entry = {
                    "time": ts_str,
                    "sender": sender,
                    "sender_id": sid,
                    "type": lt,
                    "type_name": type_names.get(lt, f"type={lt}"),
                    "content": text,
                }
                # 写入文件
                with open(FEED_FILE, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')

                # 打印（安全）
                safe_sender = sender.encode('ascii', errors='replace').decode('ascii')
                safe_text = text[:100].encode('ascii', errors='replace').decode('ascii')
                print(f"[{ts_str}] {safe_sender} | {entry['type_name']}: {safe_text}")

                info["last_ts"] = max(info["last_ts"], ts)

        time.sleep(2)

except KeyboardInterrupt:
    print("\n停止监听")
