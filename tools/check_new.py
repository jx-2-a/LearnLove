"""
快速检查新消息 — 对比上次状态，返回新消息（含语音解码）

用法:
    python tools/check_new.py --name "联系人名称"
    python tools/check_new.py --name "联系人名称"
"""
import os, sys, json, sqlite3, hashlib, re, argparse, tempfile, struct, wave
from datetime import datetime

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
LIVE_DIR = os.path.join(SCRIPT_DIR, "data", "live")
STATE_FILE = os.path.join(LIVE_DIR, "monitor_state.json")
os.makedirs(LIVE_DIR, exist_ok=True)

import zstandard as zstd
_zstd_dctx = zstd.ZstdDecompressor()

# ============ 解密（如果需要）============

PAGE_SZ = 4096
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

with open(KEYS_FILE) as f:
    ALL_KEYS = json.load(f)

from Crypto.Cipher import AES


def decrypt_page(enc_key, page_data, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ))
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def full_decrypt(db_path, out_path, enc_key):
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))


def decrypt_wal(wal_path, out_path, enc_key):
    if not os.path.exists(wal_path):
        return
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]
        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)


def refresh_decrypted(msg_rel_key):
    """重新解密消息DB"""
    rel_path = msg_rel_key.replace('\\', os.sep)
    db_path = os.path.join(DB_DIR, rel_path)
    wal_path = db_path + "-wal"
    if not os.path.exists(db_path):
        return None
    enc_key = bytes.fromhex(ALL_KEYS[msg_rel_key]["enc_key"])
    out_path = os.path.join(DECRYPTED_DIR, rel_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    full_decrypt(db_path, out_path, enc_key)
    if os.path.exists(wal_path):
        decrypt_wal(wal_path, out_path, enc_key)
    return out_path


# ============ 查找联系人 ============

def find_contact(query):
    contact_db = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
    conn = sqlite3.connect(contact_db)
    q = query.lower()
    for uname, nick, remark, alias in conn.execute(
        "SELECT username, nick_name, remark, alias FROM contact"
    ).fetchall():
        candidates = [(uname or "").lower(), (nick or "").lower(),
                       (remark or "").lower(), (alias or "").lower()]
        if any(q in c for c in candidates if c):
            conn.close()
            return uname, remark or nick or uname
    conn.close()
    return None, None


# ============ 查找消息 ============

def find_msg_table(username):
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    msg_dir = os.path.join(DECRYPTED_DIR, "message")
    results = []
    for f in sorted(os.listdir(msg_dir)):
        if not f.startswith("message_") or "fts" in f or "resource" in f or "biz" in f:
            continue
        path = os.path.join(msg_dir, f)
        try:
            conn = sqlite3.connect(path)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
            ).fetchone()
            if exists:
                results.append((path, table_name))
            conn.close()
        except:
            pass
    return results


# ============ 学习发送者映射 ============

def learn_sender_map(conn, table_name, username, display_name):
    sender_map = {}
    try:
        rows = conn.execute(
            f"SELECT real_sender_id, message_content, WCDB_CT_message_content "
            f"FROM [{table_name}] WHERE local_type IN (34, 47) LIMIT 20"
        ).fetchall()
        for sid, content, ct in rows:
            if ct and ct == 4 and isinstance(content, bytes):
                text = _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
            elif isinstance(content, bytes):
                text = content.decode('utf-8', errors='replace')
            else:
                text = str(content) if content else ''
            m = re.search(r'fromusername\s*=\s*"([^"]+)"', text)
            if m:
                from_user = m.group(1)
                if from_user == username:
                    sender_map[sid] = display_name
                else:
                    sender_map[sid] = "你"
    except:
        pass
    return sender_map


# ============ 语音解码 ============

def decode_voice_messages(messages, model_name="tiny"):
    """批量解码语音消息"""
    media_db = os.path.join(DECRYPTED_DIR, "message", "media_0.db")
    if not os.path.exists(media_db):
        return

    voice_msgs = [m for m in messages if m["local_type"] == 34]
    if not voice_msgs:
        return

    media_conn = sqlite3.connect(media_db)
    times = [m["create_time"] for m in voice_msgs]
    placeholders = ','.join('?' * len(times))
    rows = media_conn.execute(
        f"SELECT create_time, voice_data FROM VoiceInfo "
        f"WHERE create_time IN ({placeholders}) AND voice_data IS NOT NULL",
        times
    ).fetchall()
    voice_data_map = {ts: data for ts, data in rows}
    media_conn.close()

    # 加载模型（延迟）
    model = None

    for msg in voice_msgs:
        voice_data = voice_data_map.get(msg["create_time"])
        if not voice_data or b"#!SILK_V3" not in voice_data[:20]:
            msg["voice_text"] = "(无音频)" if not voice_data else "(非SILK)"
            continue

        try:
            from pilk import decode as silk_decode

            if model is None:
                from faster_whisper import WhisperModel
                model = WhisperModel(model_name, device="cuda", compute_type="float16")

            silk_path = os.path.join(LIVE_DIR, f"_v_{msg['create_time']}.silk")
            pcm_path = os.path.join(LIVE_DIR, f"_v_{msg['create_time']}.pcm")
            wav_path = os.path.join(LIVE_DIR, f"_v_{msg['create_time']}.wav")

            with open(silk_path, 'wb') as f:
                f.write(voice_data)
            silk_decode(silk_path, pcm_path)

            with open(pcm_path, 'rb') as f:
                pcm_data = f.read()
            with wave.open(wav_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(pcm_data)

            segments, info = model.transcribe(wav_path, language="zh", beam_size=5)
            msg["voice_text"] = "".join(seg.text for seg in segments)

        except Exception as e:
            msg["voice_text"] = f"(转录失败: {e})"
        finally:
            for p in [silk_path, pcm_path, wav_path]:
                try:
                    os.unlink(p)
                except:
                    pass


# ============ 主逻辑 ============

def check_new(username, display_name, model="tiny"):
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

    state_key = f"last_ts_{username}"
    last_ts = state.get(state_key, 0)

    # 刷新解密（因为 WAL 可能有新数据）
    msg_rel_key = None
    for k in ALL_KEYS:
        if k.startswith("message\\message_") and k.endswith(".db") and \
           "fts" not in k and "resource" not in k and "biz" not in k:
            msg_rel_key = k
            break
    if msg_rel_key:
        refresh_decrypted(msg_rel_key)

    # 查找消息
    tables = find_msg_table(username)
    if not tables:
        return []

    all_new = []
    for db_path, table_name in tables:
        try:
            conn = sqlite3.connect(db_path)
        except:
            continue

        # 学习发送者映射
        sender_map = learn_sender_map(conn, table_name, username, display_name)
        if not sender_map:
            sender_map = {1: display_name, 2: "你"}

        rows = conn.execute(
            f"SELECT create_time, local_type, local_id, message_content, "
            f"WCDB_CT_message_content, real_sender_id "
            f"FROM [{table_name}] WHERE create_time > ? ORDER BY create_time ASC",
            (last_ts,)
        ).fetchall()
        conn.close()

        for ts, lt, lid, content, ct, sid in rows:
            if ct and ct == 4 and isinstance(content, bytes):
                text = _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
            elif isinstance(content, bytes):
                text = content.decode('utf-8', errors='replace')
            else:
                text = str(content) if content else ""

            sender = sender_map.get(sid, f"sid={sid}")
            all_new.append({
                "create_time": ts,
                "local_type": lt,
                "local_id": lid,
                "sender_id": sid,
                "sender": sender,
                "content": text,
                "voice_text": None,
            })

    # 语音解码
    if any(m["local_type"] == 34 for m in all_new):
        decode_voice_messages(all_new, model)

    # 更新状态
    if all_new:
        last_ts = max(m["create_time"] for m in all_new)
        state[state_key] = last_ts
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)

    # 格式化输出
    results = []
    for m in all_new:
        ts_str = datetime.fromtimestamp(m["create_time"]).strftime("%Y-%m-%d %H:%M:%S")
        type_names = {1: "文本", 3: "图片", 34: "语音", 47: "表情",
                       48: "位置", 49: "文件", 50: "通话"}
        type_name = type_names.get(m["local_type"], f"type={m['local_type']}")

        body = m["content"]
        if m["local_type"] == 34:
            body = f"[语音] {m.get('voice_text', '')}"
        elif m["local_type"] != 1:
            body = f"[{type_name}]"

        results.append({
            "time": ts_str,
            "sender": m["sender"],
            "type": type_name,
            "content": body,
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="快速检查新消息")
    parser.add_argument("--name", required=True, help="联系人名称")
    parser.add_argument("--model", default="tiny", choices=["tiny", "small", "medium"])
    args = parser.parse_args()

    username, display_name = find_contact(args.name)
    if not username:
        print(f"未找到: {args.name}")
        return

    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print(f"[*] 检查 {display_name} 的新消息...")

    results = check_new(username, display_name, args.model)

    if not results:
        print("无新消息")
        return

    print(f"\n{'='*50}")
    print(f"  {len(results)} 条新消息")
    print(f"{'='*50}")
    for r in results:
        print(f"[{r['time']}] {r['sender']} | {r['type']}")
        if r['content']:
            print(f"  {r['content'][:200]}")
        print()

    # 也写入 feed 文件
    feed_file = os.path.join(LIVE_DIR, "incoming.jsonl")
    with open(feed_file, 'a', encoding='utf-8') as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


if __name__ == "__main__":
    main()
