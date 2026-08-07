"""
实时微信消息监听器 — 监听数据库变化，自动解码文字和语音

用法:
    python tools/live_monitor.py --name "谢雨欣"
    python tools/live_monitor.py --name "谢雨欣" --interval 2 --model small

架构:
    1. 监听 WeChat DB 文件 mtime 变化
    2. 检测到变化 → 重新解密 → 查询新消息
    3. 文字消息：ZSTD 解压
    4. 语音消息：SILK → pilk → faster-whisper 转录
    5. 写入 data/live/incoming.jsonl（JSON Lines，每行一条消息）

输出文件 data/live/incoming.jsonl：
    每行一个 JSON 对象，包含 time, sender, type, content, voice_text 等
    MCP 服务器的 get_live_feed 工具读取此文件返回给 Claude
"""
import os, sys, json, time, sqlite3, hashlib, argparse, tempfile, struct
from datetime import datetime
from collections import defaultdict

# ============ 路径配置 ============
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "wechat-decrypt"))

CONFIG_FILE = os.path.join(SCRIPT_DIR, "wechat-decrypt", "config.json")
with open(CONFIG_FILE) as f:
    _cfg = json.load(f)
for key in ("keys_file", "decrypted_dir"):
    if key in _cfg and not os.path.isabs(_cfg[key]):
        _cfg[key] = os.path.join(SCRIPT_DIR, "wechat-decrypt", _cfg[key])

DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
DECRYPTED_DIR = _cfg["decrypted_dir"]

LIVE_DIR = os.path.join(SCRIPT_DIR, "data", "live")
STATE_FILE = os.path.join(LIVE_DIR, "monitor_state.json")
FEED_FILE = os.path.join(LIVE_DIR, "incoming.jsonl")
os.makedirs(LIVE_DIR, exist_ok=True)

# ============ 加密常量（复用 mcp_server 逻辑）============
PAGE_SZ = 4096
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

with open(KEYS_FILE) as f:
    ALL_KEYS = json.load(f)

from Crypto.Cipher import AES
import zstandard as zstd

_zstd_dctx = zstd.ZstdDecompressor()


def decrypt_page(enc_key, page_data, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
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
    return total_pages


def decrypt_wal(wal_path, out_path, enc_key):
    if not os.path.exists(wal_path):
        return 0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    patched = 0
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
            patched += 1
    return patched


# ============ DB 缓存（同 mcp_server 逻辑）============

class DBCache:
    def __init__(self):
        self._cache = {}

    def get(self, rel_key):
        if rel_key not in ALL_KEYS:
            return None
        rel_path = rel_key.replace('\\', os.sep)
        db_path = os.path.join(DB_DIR, rel_path)
        wal_path = db_path + "-wal"
        if not os.path.exists(db_path):
            return None

        try:
            db_mtime = os.path.getmtime(db_path)
            wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        except OSError:
            return None

        if rel_key in self._cache:
            c_db_mt, c_wal_mt, c_path = self._cache[rel_key]
            if c_db_mt == db_mtime and c_wal_mt == wal_mtime and os.path.exists(c_path):
                return c_path
            try:
                os.unlink(c_path)
            except OSError:
                pass

        enc_key = bytes.fromhex(ALL_KEYS[rel_key]["enc_key"])
        fd, tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        full_decrypt(db_path, tmp_path, enc_key)
        if os.path.exists(wal_path):
            decrypt_wal(wal_path, tmp_path, enc_key)
        self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)
        return tmp_path

    def cleanup(self):
        for _, _, path in self._cache.values():
            try:
                os.unlink(path)
            except OSError:
                pass
        self._cache.clear()


# ============ 消息解码器 ============

MSG_DB_KEYS = sorted([
    k for k in ALL_KEYS
    if k.startswith("message\\message_") and k.endswith(".db")
    and "fts" not in k and "resource" not in k and "biz" not in k
])

MEDIA_DB_KEY = "message\\media_0.db"


class MessageDecoder:
    """解码文字和语音消息"""

    def __init__(self, cache, model_name="small"):
        self._cache = cache
        self._whisper_model = None
        self._model_name = model_name

    @property
    def whisper_model(self):
        if self._whisper_model is None:
            from faster_whisper import WhisperModel
            self._whisper_model = WhisperModel(
                self._model_name, device="cuda", compute_type="float16"
            )
        return self._whisper_model

    def find_user_table(self, username):
        """在所有 message_N.db 中查找用户的消息表"""
        table_hash = hashlib.md5(username.encode()).hexdigest()
        table_name = f"Msg_{table_hash}"

        for rel_key in MSG_DB_KEYS:
            path = self._cache.get(rel_key)
            if not path:
                continue
            conn = sqlite3.connect(path)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,)
                ).fetchone()
                if exists:
                    conn.close()
                    return path, table_name
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except:
                    pass
        return None, None

    def get_new_messages(self, username, since_ts):
        """获取指定用户自 since_ts 以来的新消息"""
        db_path, table_name = self.find_user_table(username)
        if not db_path:
            return []

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT create_time, local_type, local_id, message_content, "
                f"WCDB_CT_message_content, real_sender_id "
                f"FROM [{table_name}] WHERE create_time > ? ORDER BY create_time ASC",
                (since_ts,)
            ).fetchall()
        except Exception as e:
            safe_print(f"  [WARN] 查询新消息失败: {e}")
            conn.close()
            return []
        conn.close()

        messages = []
        for ts, local_type, local_id, content, ct, sender_id in rows:
            text = self._decode_text(content, ct)
            messages.append({
                "create_time": ts,
                "local_type": local_type,
                "local_id": local_id,
                "sender_id": sender_id,
                "content": text,
                "voice_text": None,
            })

        # 批量解码语音消息
        voice_msgs = [m for m in messages if m["local_type"] == 34]
        if voice_msgs:
            self._decode_voices(voice_msgs)

        return messages

    def _decode_text(self, content, ct):
        """ZSTD 解压文本消息"""
        if content is None:
            return ""
        if ct and ct == 4 and isinstance(content, bytes):
            try:
                return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
            except Exception:
                return "(解压失败)"
        if isinstance(content, bytes):
            try:
                return content.decode('utf-8', errors='replace')
            except Exception:
                return "(解码失败)"
        return str(content)

    def _decode_voices(self, voice_messages):
        """批量解码语音消息"""
        # 获取 VoiceInfo 数据
        media_path = self._cache.get(MEDIA_DB_KEY)
        if not media_path:
            return

        media_conn = sqlite3.connect(media_path)
        try:
            # 查询这些语音的 SILK 数据
            times = [m["create_time"] for m in voice_messages]
            if not times:
                return

            placeholders = ','.join('?' * len(times))
            rows = media_conn.execute(
                f"SELECT create_time, voice_data FROM VoiceInfo "
                f"WHERE create_time IN ({placeholders}) AND voice_data IS NOT NULL",
                times
            ).fetchall()

            voice_data_map = {ts: data for ts, data in rows}
        finally:
            media_conn.close()

        # 逐个解码和转录
        for msg in voice_messages:
            voice_data = voice_data_map.get(msg["create_time"])
            if not voice_data:
                msg["voice_text"] = "(无音频数据)"
                continue

            # 验证 SILK 魔数
            if b"#!SILK_V3" not in voice_data[:20]:
                msg["voice_text"] = "(非SILK格式)"
                continue

            try:
                from pilk import decode as silk_decode, get_duration

                # 写入临时文件
                silk_path = os.path.join(LIVE_DIR, f"_tmp_{msg['local_id']}.silk")
                pcm_path = os.path.join(LIVE_DIR, f"_tmp_{msg['local_id']}.pcm")
                wav_path = os.path.join(LIVE_DIR, f"_tmp_{msg['local_id']}.wav")

                with open(silk_path, 'wb') as f:
                    f.write(voice_data)

                silk_decode(silk_path, pcm_path)

                # PCM → WAV
                import wave
                with open(pcm_path, 'rb') as f:
                    pcm_data = f.read()
                with wave.open(wav_path, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(pcm_data)

                # Whisper 转录
                segments, info = self.whisper_model.transcribe(
                    wav_path, language="zh", beam_size=5
                )
                text = "".join(seg.text for seg in segments)
                msg["voice_text"] = text

            except Exception as e:
                msg["voice_text"] = f"(转录失败: {e})"
            finally:
                for p in [silk_path, pcm_path, wav_path]:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass


# ============ 安全输出（处理 Windows GBK 编码）============

def safe_print(*args, **kwargs):
    """安全打印，过滤 Windows 终端不支持的字符"""
    import io
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        cleaned = []
        for a in args:
            if isinstance(a, str):
                a = a.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                a = ''.join(c if ord(c) < 65536 else '?' for c in a)
            cleaned.append(a)
        try:
            print(*cleaned, **kwargs)
        except UnicodeEncodeError:
            print(*(str(c).encode('ascii', errors='replace').decode('ascii') for c in cleaned), **kwargs)


# ============ 实时监听主循环 ============

def load_state():
    """加载上次检查状态"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def append_to_feed(entry):
    """追加一条消息到 feed 文件"""
    with open(FEED_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def format_message(msg, sender_map):
    """格式化一条消息为可读文本"""
    ts = datetime.fromtimestamp(msg["create_time"]).strftime("%Y-%m-%d %H:%M:%S")
    sender = sender_map.get(msg["sender_id"], f"ID-{msg['sender_id']}")

    type_names = {
        1: "文本", 3: "图片", 34: "语音", 43: "视频",
        47: "表情", 48: "位置", 49: "链接/文件", 50: "VOIP",
        10000: "系统", 10002: "撤回",
    }
    type_name = type_names.get(msg["local_type"], f"类型{msg['local_type']}")

    if msg["local_type"] == 1:
        body = msg["content"]
    elif msg["local_type"] == 34:
        body = f"[语音]"
        if msg.get("voice_text"):
            body += f" {msg['voice_text']}"
    else:
        body = f"[{type_name}]"

    return {
        "time": ts,
        "sender": sender,
        "sender_id": msg["sender_id"],
        "type": msg["local_type"],
        "type_name": type_name,
        "content": msg["content"],
        "voice_text": msg.get("voice_text"),
    }


def watch_loop(username, display_name, interval=2, model="small"):
    """主监听循环"""
    cache = DBCache()
    decoder = MessageDecoder(cache, model_name=model)
    state = load_state()
    state_key = f"last_ts_{username}"
    last_ts = state.get(state_key, 0)

    # 发送者映射：先初始猜测，然后从消息内容中的 fromusername 自动校准
    sender_map = {1: display_name, 2: "你"}  # 默认值，会被实际数据覆盖

    def learn_sender_map(conn, table_name):
        """通过学习消息中的 fromusername 确定 sender_id 映射"""
        import re
        # 找几条有 fromusername 的消息（语音/表情 XML）
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
                        sender_map[sid] = display_name  # 对方
                    else:
                        sender_map[sid] = "你"  # 用户自己
            return True
        except:
            return False

    safe_print(f"[*] 开始监听 {display_name} ({username})")
    safe_print(f"    轮询间隔: {interval}s")
    safe_print(f"    Whisper模型: {model}")
    safe_print(f"    上次检查: {datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M:%S') if last_ts else '首次运行'}")
    safe_print(f"    输出文件: {FEED_FILE}")

    # 自动学习 sender_id 映射
    db_path, table_name = decoder.find_user_table(username)
    if db_path:
        conn = sqlite3.connect(db_path)
        learn_sender_map(conn, table_name)
        conn.close()
    safe_print(f"    发送者映射: {sender_map}")
    safe_print()
    safe_print("[*] 等待新消息... (Ctrl+C 停止)")
    safe_print("    语音消息到达时会自动加载Whisper模型(~2s)")
    safe_print()

    try:
        while True:
            try:
                new_msgs = decoder.get_new_messages(username, last_ts)

                if new_msgs:
                    max_ts = last_ts
                    for msg in new_msgs:
                        formatted = format_message(msg, sender_map)
                        append_to_feed(formatted)

                        # 实时打印
                        ts_short = formatted["time"][11:]  # HH:MM:SS
                        safe_print(f"[{ts_short}] {formatted['sender']} | {formatted['type_name']}")
                        if formatted["type_name"] == "文本":
                            safe_print(f"  [文字] {formatted['content'][:150]}")
                        elif formatted["type_name"] == "语音" and formatted.get("voice_text"):
                            safe_print(f"  [语音] {formatted['voice_text'][:150]}")
                        safe_print()

                        if msg["create_time"] > max_ts:
                            max_ts = msg["create_time"]

                    last_ts = max_ts
                    state[state_key] = last_ts
                    save_state(state)
                    safe_print(f"  [OK] 已处理 {len(new_msgs)} 条新消息\n")

            except Exception as e:
                safe_print(f"  [ERROR] 检查消息时出错: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(interval)

    except KeyboardInterrupt:
        safe_print("\n[*] 监听已停止")
    finally:
        cache.cleanup()
        save_state(state)
        safe_print(f"状态已保存到: {STATE_FILE}")


def main():
    parser = argparse.ArgumentParser(description="微信实时消息监听器")
    parser.add_argument("--name", required=True, help="监听的联系人名称")
    parser.add_argument("--username", help="直接指定 wxid（跳过搜索）")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数 (默认2)")
    parser.add_argument("--model", default="small", choices=["tiny", "small", "medium"],
                        help="Whisper模型大小 (默认small)")
    args = parser.parse_args()

    # 查找用户
    if args.username:
        username = args.username
        display_name = args.name
    else:
        # 从 contact 数据库查找
        contact_db = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
        if not os.path.exists(contact_db):
            safe_print("[ERROR] 未解密 contact.db，请先运行 decrypt_db.py")
            return

        conn = sqlite3.connect(contact_db)
        query = args.name.lower()
        username = None
        display_name = args.name

        rows = conn.execute("SELECT username, nick_name, remark, alias FROM contact").fetchall()
        for uname, nick, remark, alias in rows:
            candidates = [
                (uname or "").lower(),
                (nick or "").lower(),
                (remark or "").lower(),
                (alias or "").lower(),
            ]
            if any(query in c for c in candidates if c):
                username = uname
                display_name = remark or nick or uname
                break
        conn.close()

        if not username:
            # 也检查 SessionTable
            session_db = os.path.join(DECRYPTED_DIR, "session", "session.db")
            if os.path.exists(session_db):
                conn = sqlite3.connect(session_db)
                rows = conn.execute(
                    "SELECT username FROM SessionTable WHERE username != 'brandsessionholder'"
                ).fetchall()
                conn.close()
                for (uname,) in rows:
                    if query in uname.lower():
                        username = uname
                        display_name = uname
                        break

    if not username:
        safe_print(f"[ERROR] 未找到 '{args.name}'，请先运行 decrypt_db.py 或检查名字")
        return

    safe_print(f"[+] 目标: {display_name} ({username})")
    watch_loop(username, display_name, args.interval, args.model)


if __name__ == "__main__":
    main()
