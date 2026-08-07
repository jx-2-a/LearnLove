"""
微信实时消息监听器 - Web UI (SSE推送 + mtime检测)

http://localhost:5678
- 30ms轮询WAL/DB文件的mtime变化（WAL是预分配固定大小，不能用size检测）
- 检测到变化后：全量解密DB + 全量WAL patch
- SSE 服务器推送
"""
import hashlib, struct, os, sys, json, time, sqlite3, io, threading, queue, traceback
import hmac as hmac_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from Crypto.Cipher import AES
import urllib.parse
import glob as glob_mod
import zstandard as zstd
from decode_image import extract_md5_from_packed_info, decrypt_dat_file, is_v2_format

_zstd_dctx = zstd.ZstdDecompressor()

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

from config import load_config
_cfg = load_config()
DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
CONTACT_CACHE = os.path.join(_cfg["decrypted_dir"], "contact", "contact.db")
DECRYPTED_SESSION = os.path.join(_cfg["decrypted_dir"], "session", "session.db")
DECODED_IMAGE_DIR = _cfg.get("decoded_image_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decoded_images"))
MONITOR_CACHE_DIR = os.path.join(_cfg["decrypted_dir"], "_monitor_cache")
WECHAT_BASE_DIR = _cfg.get("wechat_base_dir", "")
IMAGE_AES_KEY = _cfg.get("image_aes_key")  # V2 格式 AES key (从微信内存提取)
IMAGE_XOR_KEY = _cfg.get("image_xor_key", 0x88)  # XOR key

POLL_MS = 30  # 高频轮询WAL/DB的mtime，30ms一次
PORT = 5678

sse_clients = []
sse_lock = threading.Lock()
messages_log = []
messages_lock = threading.Lock()
MAX_LOG = 500
_img_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='img')


class MonitorDBCache:
    """轻量 DB 缓存，mtime 检测变化时重新解密"""

    def __init__(self, keys, tmp_dir):
        self.keys = keys
        self.tmp_dir = tmp_dir
        os.makedirs(tmp_dir, exist_ok=True)
        self._state = {}  # rel_key → (db_mtime, wal_mtime)

    def get(self, rel_key):
        """返回解密后的临时文件路径，mtime 变化时自动重新解密"""
        if rel_key not in self.keys:
            return None

        enc_key = bytes.fromhex(self.keys[rel_key]["enc_key"])
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

        out_name = rel_key.replace('\\', '_')
        out_path = os.path.join(self.tmp_dir, out_name)

        prev = self._state.get(rel_key)

        if prev is None or db_mtime != prev[0]:
            t0 = time.perf_counter()
            full_decrypt(db_path, out_path, enc_key)
            if os.path.exists(wal_path):
                decrypt_wal_full(wal_path, out_path, enc_key)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  [cache] {rel_key} 全量解密 {ms:.0f}ms", flush=True)
            self._state[rel_key] = (db_mtime, wal_mtime)
        elif wal_mtime != prev[1]:
            t0 = time.perf_counter()
            decrypt_wal_full(wal_path, out_path, enc_key)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  [cache] {rel_key} WAL patch {ms:.0f}ms", flush=True)
            self._state[rel_key] = (db_mtime, wal_mtime)

        return out_path


def build_username_db_map():
    """从已解密的 Name2Id 表构建 username → [db_keys] 映射

    同一个 username 可能存在于多个 message_N.db 中,
    按 DB 文件修改时间倒序排列（最新的排前面）。
    """
    # 先获取每个 DB 的 mtime 用于排序
    db_mtimes = {}
    for i in range(5):
        rel_key = f"message\\message_{i}.db"
        db_path = os.path.join(DB_DIR, "message", f"message_{i}.db")
        try:
            db_mtimes[rel_key] = os.path.getmtime(db_path)
        except OSError:
            db_mtimes[rel_key] = 0

    mapping = {}  # username → [db_keys], 最新的在前
    decrypted_msg_dir = os.path.join(_cfg["decrypted_dir"], "message")
    for i in range(5):
        db_path = os.path.join(decrypted_msg_dir, f"message_{i}.db")
        if not os.path.exists(db_path):
            continue
        rel_key = f"message\\message_{i}.db"
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for row in conn.execute("SELECT user_name FROM Name2Id").fetchall():
                if row[0] not in mapping:
                    mapping[row[0]] = []
                mapping[row[0]].append(rel_key)
            conn.close()
        except Exception as e:
            print(f"  [WARN] Name2Id message_{i}.db: {e}", flush=True)

    # 对每个 username 的 db_keys 按 mtime 倒序（最新的优先）
    for username in mapping:
        mapping[username].sort(key=lambda k: db_mtimes.get(k, 0), reverse=True)

    return mapping


def decrypt_page(enc_key, page_data, pgno):
    """解密单个加密页面"""
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def full_decrypt(db_path, out_path, enc_key):
    """首次全量解密"""
    t0 = time.perf_counter()
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ

    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))

    ms = (time.perf_counter() - t0) * 1000
    return total_pages, ms


def decrypt_wal_full(wal_path, out_path, enc_key):
    """解密WAL当前有效frame，patch到已解密的DB副本

    WAL是预分配固定大小(4MB)，包含当前有效frame和上一轮遗留的旧frame。
    通过WAL header中的salt值区分：只有frame header的salt匹配WAL header的才是有效frame。

    返回: (patched_pages, elapsed_ms)
    """
    t0 = time.perf_counter()

    if not os.path.exists(wal_path):
        return 0, 0

    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0, 0

    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ  # 24 + 4096 = 4120
    patched = 0

    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        # 读WAL header，获取当前salt值
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

            # 校验: pgno有效 且 salt匹配当前WAL周期
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue  # 旧周期遗留的frame，跳过

            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1

    ms = (time.perf_counter() - t0) * 1000
    return patched, ms


def load_contact_names():
    names = {}
    try:
        conn = sqlite3.connect(CONTACT_CACHE)
        for r in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            names[r[0]] = r[2] if r[2] else r[1] if r[1] else r[0]
        conn.close()
    except:
        pass
    return names


def format_msg_type(t):
    return {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }.get(t, f'type={t}')


def msg_type_icon(t):
    return {
        1: '💬', 3: '🖼️', 34: '🎤', 42: '👤',
        43: '🎬', 47: '😀', 48: '📍', 49: '🔗',
        50: '📞', 10000: '⚙️', 10002: '↩️',
    }.get(t, '📨')


def broadcast_sse(msg_data):
    event_type = msg_data.get('event', '')
    data_line = f"data: {json.dumps(msg_data, ensure_ascii=False)}\n"
    if event_type:
        payload = f"event: {event_type}\n{data_line}\n"
    else:
        payload = f"{data_line}\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


# ============ 监听器 ============

class SessionMonitor:
    def __init__(self, enc_key, session_db, contact_names, db_cache=None, username_db_map=None):
        self.enc_key = enc_key
        self.session_db = session_db
        self.wal_path = session_db + "-wal"
        self.contact_names = contact_names
        self.db_cache = db_cache
        self.username_db_map = username_db_map or {}
        self.prev_state = {}
        self.decrypt_ms = 0
        self.patched_pages = 0

    def resolve_image(self, username, timestamp):
        """解密图片: username+timestamp → 解密后的图片文件名，失败返回 None"""
        if not self.db_cache or not self.username_db_map:
            return None

        # 1. 找到 username 对应的所有 message_N.db（按 mtime 倒序）
        db_keys = self.username_db_map.get(username)
        if not db_keys:
            return None

        # 2. 遍历候选 DB，找到包含该 timestamp 消息的那个
        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        local_id = None
        for db_key in db_keys:
            msg_db_path = self.db_cache.get(db_key)
            if not msg_db_path:
                continue
            try:
                conn = sqlite3.connect(f"file:{msg_db_path}?mode=ro", uri=True)
                # 精确匹配 timestamp
                row = conn.execute(f"""
                    SELECT local_id FROM [{table_name}]
                    WHERE local_type = 3 AND create_time = ?
                """, (timestamp,)).fetchone()
                if not row:
                    # 模糊匹配（±3秒内最近的图片消息）
                    row = conn.execute(f"""
                        SELECT local_id FROM [{table_name}]
                        WHERE local_type = 3 AND ABS(create_time - ?) <= 3
                        ORDER BY ABS(create_time - ?) LIMIT 1
                    """, (timestamp, timestamp)).fetchone()
                conn.close()
                if row:
                    local_id = row[0]
                    break
            except Exception as e:
                print(f"  [img] 查询 {db_key}/{table_name} 失败: {e}", flush=True)

        if not local_id:
            print(f"  [img] 未找到 local_id: {username} t={timestamp}", flush=True)
            return None

        # 4. 查 message_resource.db 获取 MD5
        #    local_id 不全局唯一，需要同时匹配 create_time
        res_path = self.db_cache.get("message\\message_resource.db")
        if not res_path:
            return None

        file_md5 = None
        try:
            conn = sqlite3.connect(f"file:{res_path}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT packed_info FROM MessageResourceInfo "
                "WHERE message_local_id = ? AND message_create_time = ? AND message_local_type = 3",
                (local_id, timestamp)
            ).fetchone()
            if not row:
                # 降级: 只用 create_time + type
                row = conn.execute(
                    "SELECT packed_info FROM MessageResourceInfo "
                    "WHERE message_create_time = ? AND message_local_type = 3",
                    (timestamp,)
                ).fetchone()
            conn.close()
            if row and row[0]:
                file_md5 = extract_md5_from_packed_info(row[0])
        except Exception as e:
            print(f"  [img] 查询 message_resource 失败: {e}", flush=True)
            return None

        if not file_md5:
            print(f"  [img] 未找到 MD5: local_id={local_id} t={timestamp}", flush=True)
            return None

        # 5. 查找 .dat 文件
        attach_dir = os.path.join(WECHAT_BASE_DIR, "msg", "attach")
        username_hash = hashlib.md5(username.encode()).hexdigest()
        search_base = os.path.join(attach_dir, username_hash)

        if not os.path.isdir(search_base):
            print(f"  [img] attach 目录不存在: {search_base}", flush=True)
            return None

        pattern = os.path.join(search_base, "*", "Img", f"{file_md5}*.dat")
        dat_files = sorted(glob_mod.glob(pattern))
        if not dat_files:
            print(f"  [img] 未找到 .dat: MD5={file_md5}", flush=True)
            return None

        # 优先原图，然后高清 _h，最后缩略图 _t
        selected = dat_files[0]
        for f in dat_files:
            fname = os.path.basename(f)
            if not fname.startswith(file_md5 + '_'):
                selected = f
                break
        for f in dat_files:
            if f.endswith('_h.dat'):
                selected = f
                break

        # 6. 解密图片
        os.makedirs(DECODED_IMAGE_DIR, exist_ok=True)
        out_base = os.path.join(DECODED_IMAGE_DIR, file_md5)

        # 已解密则跳过
        for ext in ('jpg', 'png', 'gif', 'webp', 'bmp', 'tif'):
            candidate = f"{out_base}.{ext}"
            if os.path.exists(candidate):
                return os.path.basename(candidate)

        # V2 新格式需要 AES key
        if is_v2_format(selected) and not IMAGE_AES_KEY:
            print(f"  [img] V2 格式缺少 AES key: {os.path.basename(selected)}", flush=True)
            print(f"  [img] 请运行 find_image_key.py 提取密钥", flush=True)
            return '__v2_unsupported__'

        result_path, fmt = decrypt_dat_file(selected, f"{out_base}.tmp", IMAGE_AES_KEY, IMAGE_XOR_KEY)
        if not result_path:
            print(f"  [img] 解密失败: {selected}", flush=True)
            return None

        final = f"{out_base}.{fmt}"
        if os.path.exists(final):
            os.unlink(final)
        os.rename(result_path, final)
        size_kb = os.path.getsize(final) / 1024
        print(f"  [img] 解密成功: {os.path.basename(final)} ({size_kb:.0f}KB)", flush=True)
        return os.path.basename(final)

    def _async_resolve_image(self, username, timestamp, msg_data):
        """后台线程: 解密图片并通过 SSE 推送更新"""
        for attempt in range(3):
            try:
                img_name = self.resolve_image(username, timestamp)
                if img_name == '__v2_unsupported__':
                    # V2 新加密格式，显示占位提示
                    msg_data['content'] = '[图片 - 新加密格式暂不支持预览]'
                    broadcast_sse({
                        'event': 'image_update',
                        'timestamp': timestamp,
                        'username': username,
                        'v2_unsupported': True,
                    })
                    return
                elif img_name:
                    image_url = f'/img/{img_name}'
                    msg_data['image_url'] = image_url
                    broadcast_sse({
                        'event': 'image_update',
                        'timestamp': timestamp,
                        'username': username,
                        'image_url': image_url,
                    })
                    print(f"  [img] 异步解密成功: {img_name}", flush=True)
                    return
                elif attempt < 2:
                    time.sleep(1.5)
            except Exception as e:
                print(f"  [img] 异步解密失败(attempt={attempt}): {e}", flush=True)
                if attempt < 2:
                    time.sleep(1.5)

    def query_state(self):
        """查询已解密副本的session状态"""
        conn = sqlite3.connect(f"file:{DECRYPTED_SESSION}?mode=ro", uri=True)
        state = {}
        for r in conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable WHERE last_timestamp > 0
        """).fetchall():
            state[r[0]] = {
                'unread': r[1], 'summary': r[2] or '', 'timestamp': r[3],
                'msg_type': r[4], 'sender': r[5] or '', 'sender_name': r[6] or '',
            }
        conn.close()
        return state

    def do_full_refresh(self):
        """全量解密DB + 全量WAL patch"""
        # 先解密主DB
        pages, ms = full_decrypt(self.session_db, DECRYPTED_SESSION, self.enc_key)
        total_ms = ms
        wal_patched = 0

        # 再patch所有WAL frames
        if os.path.exists(self.wal_path):
            wal_patched, ms2 = decrypt_wal_full(self.wal_path, DECRYPTED_SESSION, self.enc_key)
            total_ms += ms2

        self.decrypt_ms = total_ms
        self.patched_pages = pages + wal_patched
        return self.patched_pages

    def check_updates(self):
        global messages_log
        try:
            t0 = time.perf_counter()
            self.do_full_refresh()
            t1 = time.perf_counter()
            curr_state = self.query_state()
            t2 = time.perf_counter()
            print(f"  [perf] decrypt={self.patched_pages}页/{(t1-t0)*1000:.1f}ms, query={(t2-t1)*1000:.1f}ms", flush=True)
        except Exception as e:
            print(f"  [ERROR] check_updates: {e}", flush=True)
            return

        # 收集所有新消息，按时间排序后再推送
        new_msgs = []
        for username, curr in curr_state.items():
            prev = self.prev_state.get(username)
            if prev and curr['timestamp'] > prev['timestamp']:
                display = self.contact_names.get(username, username)
                is_group = '@chatroom' in username
                sender = ''
                if is_group:
                    sender = self.contact_names.get(curr['sender'], curr['sender_name'] or curr['sender'])

                summary = curr['summary']
                if isinstance(summary, bytes):
                    try:
                        summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                    except Exception:
                        summary = '(压缩内容)'
                if summary and ':\n' in summary:
                    summary = summary.split(':\n', 1)[1]

                msg_data = {
                    'time': datetime.fromtimestamp(curr['timestamp']).strftime('%H:%M:%S'),
                    'timestamp': curr['timestamp'],
                    'chat': display,
                    'username': username,
                    'is_group': is_group,
                    'sender': sender,
                    'type': format_msg_type(curr['msg_type']),
                    'type_icon': msg_type_icon(curr['msg_type']),
                    'content': summary,
                    'unread': curr['unread'],
                    'decrypt_ms': round(self.decrypt_ms, 1),
                    'pages': self.patched_pages,
                }

                new_msgs.append(msg_data)

                # 图片消息: 后台异步解密（不阻塞轮询）
                if curr['msg_type'] == 3:
                    _img_executor.submit(
                        self._async_resolve_image,
                        username, curr['timestamp'], msg_data
                    )

        # 按时间排序
        new_msgs.sort(key=lambda m: m['timestamp'])

        for msg in new_msgs:
            with messages_lock:
                messages_log.append(msg)
                if len(messages_log) > MAX_LOG:
                    messages_log = messages_log[-MAX_LOG:]

            broadcast_sse(msg)

            try:
                now = time.time()
                msg_age = now - msg['timestamp']
                tag = f"{self.patched_pages}pg/{self.decrypt_ms:.0f}ms"
                sender = msg['sender']
                now_str = datetime.fromtimestamp(now).strftime('%H:%M:%S')
                if sender:
                    print(f"[{msg['time']} 延迟={msg_age:.1f}s] [{msg['chat']}] {sender}: {msg['content']}  ({tag})", flush=True)
                else:
                    print(f"[{msg['time']} 延迟={msg_age:.1f}s] [{msg['chat']}] {msg['content']}  ({tag})", flush=True)
            except Exception:
                pass  # Windows CMD编码问题，不影响SSE推送

        self.prev_state = curr_state

def monitor_thread(enc_key, session_db, contact_names, db_cache=None, username_db_map=None):
    mon = SessionMonitor(enc_key, session_db, contact_names, db_cache, username_db_map)
    wal_path = mon.wal_path

    # 初始全量解密
    pages, ms = full_decrypt(session_db, DECRYPTED_SESSION, enc_key)
    wal_patched = 0
    wal_ms = 0
    if os.path.exists(wal_path):
        wal_patched, wal_ms = decrypt_wal_full(wal_path, DECRYPTED_SESSION, enc_key)
        print(f"[init] DB {pages}页/{ms:.0f}ms + WAL {wal_patched}页/{wal_ms:.0f}ms", flush=True)
    else:
        print(f"[init] DB {pages}页/{ms:.0f}ms", flush=True)

    mon.prev_state = mon.query_state()
    print(f"[monitor] 跟踪 {len(mon.prev_state)} 个会话", flush=True)
    print(f"[monitor] mtime轮询模式 (每{POLL_MS}ms)", flush=True)

    # mtime-based 轮询: WAL是预分配固定大小，不能用size检测
    poll_interval = POLL_MS / 1000
    prev_wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
    prev_db_mtime = os.path.getmtime(session_db)

    while True:
        time.sleep(poll_interval)
        try:
            # 用mtime检测WAL和DB变化
            try:
                wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
                db_mtime = os.path.getmtime(session_db)
            except OSError:
                continue

            if wal_mtime == prev_wal_mtime and db_mtime == prev_db_mtime:
                continue  # 无变化

            t_detect = time.perf_counter()
            wal_changed = wal_mtime != prev_wal_mtime
            db_changed = db_mtime != prev_db_mtime

            mon.check_updates()

            t_done = time.perf_counter()
            try:
                detect_str = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                print(f"  [{detect_str}] WAL={'变' if wal_changed else '-'} DB={'变' if db_changed else '-'} 总耗时={(t_done-t_detect)*1000:.1f}ms", flush=True)
            except Exception:
                pass

            prev_wal_mtime = wal_mtime
            prev_db_mtime = db_mtime

        except Exception as e:
            print(f"[poll] 错误: {e}", flush=True)
            time.sleep(1)


# ============ Web ============

HTML_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>微信消息监听</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0a0a0f;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
.header{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:14px 24px;border-bottom:1px solid rgba(255,255,255,.08);display:flex;align-items:center;gap:12px;flex-shrink:0}
.header h1{font-size:18px;font-weight:600;background:linear-gradient(90deg,#4fc3f7,#81c784);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.status{font-size:12px;padding:4px 10px;border-radius:12px;transition:all .3s}
.status.ok{background:rgba(76,175,80,.15);color:#81c784;border:1px solid rgba(76,175,80,.3)}
.status.ok::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:#4caf50;margin-right:6px;animation:pulse 2s infinite}
.status.err{background:rgba(244,67,54,.15);color:#ef9a9a;border:1px solid rgba(244,67,54,.3)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.stats{margin-left:auto;font-size:12px;color:#666;display:flex;gap:16px}
.messages{flex:1;overflow-y:auto;padding:12px}
.msg{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 14px;margin-bottom:5px;transition:transform .3s ease}
.msg:hover{background:rgba(255,255,255,.05)}
.msg.hl{border-left:3px solid #4fc3f7;background:rgba(79,195,247,.05);animation:slideIn .3s cubic-bezier(.22,1,.36,1)}
@keyframes slideIn{from{opacity:0;transform:translateY(-20px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
.msg-header{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.msg-time{font-size:11px;color:#555;font-family:"SF Mono",Monaco,monospace;min-width:55px}
.msg-chat{font-weight:600;color:#4fc3f7;font-size:13px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msg-chat.grp{color:#ce93d8}
.msg-sender{font-size:12px;color:#999}
.msg-r{margin-left:auto;display:flex;gap:6px;align-items:center}
.msg-type{font-size:10px;padding:2px 5px;border-radius:3px;background:rgba(255,255,255,.06);color:#777}
.msg-unread{font-size:10px;padding:1px 6px;border-radius:8px;background:rgba(244,67,54,.2);color:#ef9a9a;font-weight:600}
.msg-perf{font-size:9px;color:#333}
.msg-content{font-size:13px;line-height:1.4;color:#bbb;word-break:break-all;padding-left:63px}
.msg-img{max-width:300px;max-height:200px;border-radius:8px;cursor:pointer;margin-top:4px;transition:transform .2s}
.msg-img:hover{transform:scale(1.02)}
.empty{text-align:center;padding:80px 20px;color:#444}
.empty .icon{font-size:48px;margin-bottom:12px}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:2px}
</style>
</head>
<body>
<div class="header">
<h1>WeChat Monitor</h1>
<div class="status ok" id="st">SSE 实时</div>
<div class="stats"><span id="cnt">0 消息</span><span id="perf"></span></div>
</div>
<div class="messages" id="msgs">
<div class="empty" id="empty"><div class="icon">📡</div><p>等待新消息...</p><p style="margin-top:6px;font-size:11px;color:#333">WAL增量解密 · SSE推送</p></div>
</div>
<script>
let n=0;
const M=document.getElementById('msgs'), S=document.getElementById('st');
const seen = new Set();  // 去重: timestamp+username
let sseReady = false;

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

function addMsg(m, animate){
  // 去重
  const key = m.timestamp + '|' + (m.username||m.chat);
  if(seen.has(key)) return;
  seen.add(key);

  const x=document.getElementById('empty');
  if(x) x.remove();

  n++;
  document.getElementById('cnt').textContent=n+' 消息';
  if(m.decrypt_ms!=null) document.getElementById('perf').textContent=m.pages+'页/'+m.decrypt_ms+'ms';

  const d=document.createElement('div');
  d.className = animate ? 'msg hl' : 'msg';

  const sn=m.sender?`<span class="msg-sender">${esc(m.sender)}</span>`:'';
  const ur=m.unread>0?`<span class="msg-unread">${m.unread}</span>`:'';
  const cc=m.is_group?'msg-chat grp':'msg-chat';

  let contentHtml = esc(m.content||'');
  if(m.image_url){
    contentHtml = `<img class="msg-img" src="${m.image_url}" onclick="window.open('${m.image_url}','_blank')" onerror="this.style.display='none';this.nextElementSibling.style.display='inline'" /><span style="display:none">${esc(m.content||'')}</span>`;
  }

  const dk=m.timestamp+'|'+(m.username||m.chat);
  d.innerHTML=`<div class="msg-header"><span class="msg-time">${m.time}</span><span class="${cc}">${esc(m.chat)}</span>${sn}<div class="msg-r"><span class="msg-type">${m.type_icon} ${m.type}</span>${ur}</div></div><div class="msg-content" data-key="${dk}">${contentHtml}</div>`;

  M.insertBefore(d, M.firstChild);

  if(animate){
    setTimeout(()=>d.classList.remove('hl'), 3000);
    document.title='('+n+') 微信监听';
  }

  // 限制最多200条
  while(M.children.length>200) M.removeChild(M.lastChild);
}

function connectSSE(){
  const es=new EventSource('/stream');
  es.onopen=()=>{
    S.textContent='SSE 实时';
    S.className='status ok';
    sseReady=true;
  };
  es.onmessage=ev=>{
    addMsg(JSON.parse(ev.data), true);  // 新消息有动画
  };
  es.addEventListener('image_update', ev=>{
    const d=JSON.parse(ev.data);
    const key=d.timestamp+'|'+(d.username||'');
    const msgs=M.querySelectorAll('.msg');
    for(const el of msgs){
      const ct=el.querySelector('.msg-content');
      if(ct && ct.dataset.key===key){
        if(d.v2_unsupported){
          ct.innerHTML='<span style="color:#999;font-style:italic">[图片 - 新加密格式暂不支持预览]</span>';
        } else if(d.image_url){
          ct.innerHTML=`<img class="msg-img" src="${d.image_url}" onclick="window.open('${d.image_url}','_blank')" onerror="this.style.display='none'" />`;
        }
        break;
      }
    }
  });
  es.onerror=()=>{
    S.textContent='重连...';
    S.className='status err';
    sseReady=false;
    es.close();
    setTimeout(connectSSE, 2000);  // 重连不清页面
  };
}

// 启动: 加载历史(无动画) → 连接SSE(有动画)
fetch('/api/history').then(r=>r.json()).then(ms=>{
  ms.sort((a,b)=>a.timestamp-b.timestamp);
  ms.forEach(m=>addMsg(m, false));  // 历史消息无动画
  connectSSE();
});
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # 浏览器关闭连接，正常

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))

        elif self.path == '/api/history':
            with messages_lock:
                data = sorted(messages_log, key=lambda m: m.get('timestamp', 0))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

        elif self.path.startswith('/img/'):
            filename = urllib.parse.unquote(self.path[5:])
            # 安全: 防目录穿越
            if '/' in filename or '\\' in filename or '..' in filename:
                self.send_error(403)
                return
            filepath = os.path.join(DECODED_IMAGE_DIR, filename)
            if not os.path.isfile(filepath):
                self.send_error(404)
                return
            ext = os.path.splitext(filename)[1].lower()
            ct = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
                '.webp': 'image/webp', '.bmp': 'image/bmp',
                '.tif': 'image/tiff',
            }.get(ext, 'application/octet-stream')
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(data)

        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()

            q = queue.Queue()
            with sse_lock:
                sse_clients.append(q)
            try:
                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(payload.encode('utf-8'))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b': hb\n\n')
                        self.wfile.flush()
            except:
                pass
            finally:
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)
        else:
            self.send_error(404)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    print("=" * 60, flush=True)
    print("  微信实时监听 (WAL增量 + SSE推送)", flush=True)
    print("=" * 60, flush=True)

    with open(KEYS_FILE) as f:
        keys = json.load(f)

    enc_key = bytes.fromhex(keys["session\\session.db"]["enc_key"])
    session_db = os.path.join(DB_DIR, "session", "session.db")

    print("加载联系人...", flush=True)
    contact_names = load_contact_names()
    print(f"已加载 {len(contact_names)} 个联系人", flush=True)

    print("构建 username→DB 映射...", flush=True)
    username_db_map = build_username_db_map()
    print(f"已映射 {len(username_db_map)} 个用户名", flush=True)

    db_cache = MonitorDBCache(keys, MONITOR_CACHE_DIR)

    # 后台预热 message_resource.db（图片解密必需）
    def _warmup():
        t0 = time.perf_counter()
        db_cache.get("message\\message_resource.db")
        print(f"[warmup] message_resource.db 预热完成 {(time.perf_counter()-t0)*1000:.0f}ms", flush=True)
    threading.Thread(target=_warmup, daemon=True).start()

    t = threading.Thread(target=monitor_thread, args=(enc_key, session_db, contact_names, db_cache, username_db_map), daemon=True)
    t.start()

    server = ThreadedServer(('0.0.0.0', PORT), Handler)
    print(f"\n=> http://localhost:{PORT}", flush=True)
    print("Ctrl+C 停止\n", flush=True)

    try:
        os.system(f'cmd.exe /c start http://localhost:{PORT}')
    except:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == '__main__':
    main()
