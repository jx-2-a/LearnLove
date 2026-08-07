"""快速测试 live_monitor 的数据库访问和消息查询是否正常（不加载Whisper）"""
import os, sys, json, sqlite3, hashlib

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "wechat-decrypt"))

CONFIG_FILE = os.path.join(SCRIPT_DIR, "wechat-decrypt", "config.json")
with open(CONFIG_FILE) as f:
    _cfg = json.load(f)
for key in ("keys_file", "decrypted_dir"):
    if key in _cfg and not os.path.isabs(_cfg[key]):
        _cfg[key] = os.path.join(SCRIPT_DIR, "wechat-decrypt", _cfg[key])

DECRYPTED_DIR = _cfg["decrypted_dir"]
DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]

with open(KEYS_FILE) as f:
    ALL_KEYS = json.load(f)

print("=== 数据库访问测试 ===")
print(f"DB_DIR: {DB_DIR}")
print(f"DECRYPTED_DIR: {DECRYPTED_DIR}")

# 1. 检查解密后的 DB
print("\n[1] 解密后数据库:")
for area in ["message", "contact", "session"]:
    area_dir = os.path.join(DECRYPTED_DIR, area)
    if os.path.isdir(area_dir):
        dbs = [f for f in os.listdir(area_dir) if f.endswith('.db')]
        print(f"  {area}/: {len(dbs)} 个DB — {dbs}")

# 2. 查找谢雨欣
print("\n[2] 查找谢雨欣:")
contact_db = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
username = None
conn = sqlite3.connect(contact_db)
rows = conn.execute("SELECT username, remark, nick_name FROM contact").fetchall()
for uname, remark, nick in rows:
    if '谢雨欣' in str(remark) or '谢雨欣' in str(nick):
        username = uname
        # 安全打印，过滤 emoji
        safe_nick = (nick or '').encode('ascii', errors='replace').decode('ascii')
        safe_remark = (remark or '').encode('ascii', errors='replace').decode('ascii')
        print(f"  username={uname}  remark={safe_remark}  nick={safe_nick}")
conn.close()

if not username:
    print("  [FAIL] 未找到谢雨欣!")
    sys.exit(1)

# 3. 查找消息表
print("\n[3] 查找消息表:")
table_hash = hashlib.md5(username.encode()).hexdigest()
table_name = f"Msg_{table_hash}"
print(f"  table_name={table_name}")

msg_dir = os.path.join(DECRYPTED_DIR, "message")
found_db = None
for f in sorted(os.listdir(msg_dir)):
    if not f.startswith("message_") or "fts" in f or "resource" in f or "biz" in f:
        continue
    path = os.path.join(msg_dir, f)
    c = sqlite3.connect(path)
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    if exists:
        found_db = path
        cnt = c.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
        latest = c.execute(f"SELECT MAX(create_time) FROM [{table_name}]").fetchone()[0]
        from datetime import datetime
        print(f"  DB: {f} — {cnt} 条消息 — 最新: {datetime.fromtimestamp(latest).strftime('%Y-%m-%d %H:%M:%S')}")
    c.close()

if not found_db:
    print("  [FAIL] 未找到消息表!")
    sys.exit(1)

# 4. 检查 VoiceInfo 覆盖
print("\n[4] 检查语音数据:")
media_db = os.path.join(msg_dir, "media_0.db")
if os.path.exists(media_db):
    mc = sqlite3.connect(media_db)
    # 通过时间匹配找到 chat_name_id
    c2 = sqlite3.connect(found_db)
    voice_times = c2.execute(f"SELECT create_time FROM [{table_name}] WHERE local_type=34 ORDER BY create_time DESC LIMIT 50").fetchall()
    c2.close()
    times = [t[0] for t in voice_times]
    for chat_id in range(1, 10):
        matches = mc.execute(
            f"SELECT COUNT(*) FROM VoiceInfo WHERE chat_name_id={chat_id} AND create_time IN ({','.join('?'*len(times))})",
            times
        ).fetchone()[0]
        if matches > 0:
            total = mc.execute("SELECT COUNT(*) FROM VoiceInfo WHERE chat_name_id=?", (chat_id,)).fetchone()[0]
            print(f"  chat_name_id={chat_id}: {matches}/{len(times)} 时间匹配, 共{total}条语音")
    mc.close()
else:
    print(f"  media_0.db 不存在: {media_db}")

# 5. 检查新消息查询
print("\n[5] 测试新消息查询 (since 1 hour ago):")
import time
since_ts = int(time.time()) - 3600
c = sqlite3.connect(found_db)
import zstandard as zstd
dctx = zstd.ZstdDecompressor()
rows = c.execute(
    f"SELECT create_time, local_type, local_id, message_content, WCDB_CT_message_content, real_sender_id "
    f"FROM [{table_name}] WHERE create_time > ? ORDER BY create_time DESC",
    (since_ts,)
).fetchall()
c.close()

from datetime import datetime
for ts, lt, lid, content, ct, sid in rows:
    if ct and ct == 4 and isinstance(content, bytes):
        text = dctx.decompress(content).decode('utf-8', errors='replace')
    elif isinstance(content, bytes):
        text = content.decode('utf-8', errors='replace')
    else:
        text = str(content)
    ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
    type_name = {1: '文本', 3: '图片', 34: '语音', 47: '表情'}.get(lt, f'type={lt}')
    sender = '谢雨欣' if sid == 1 else ('你' if sid == 2 else f'ID-{sid}')
    print(f"  [{ts_str}] {sender} | {type_name}: {text[:80]}")

print(f"\n[OK] 所有检查通过！live_monitor.py 可以正常运行")
print(f"\n启动命令:")
print(f"  source .venv/Scripts/activate")
print(f"  python tools/live_monitor.py --name '谢雨欣' --model tiny")
