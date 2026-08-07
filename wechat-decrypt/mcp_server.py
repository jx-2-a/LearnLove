r"""
WeChat MCP Server - query WeChat messages, contacts via Claude

Based on FastMCP (stdio transport), reuses existing decryption.
Runs on Windows Python (needs access to D:\ WeChat databases).
"""

import os, sys, json, time, sqlite3, tempfile, struct, hashlib, atexit
import hmac as hmac_mod
from datetime import datetime
from Crypto.Cipher import AES
from mcp.server.fastmcp import FastMCP
import zstandard as zstd
from decode_image import ImageResolver

# ============ 加密常量 ============
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

# ============ 配置加载 ============
from config import load_config

_cfg = load_config()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
DECRYPTED_DIR = _cfg["decrypted_dir"]
WECHAT_BASE_DIR = _cfg["wechat_base_dir"]
DECODED_IMAGE_DIR = _cfg["decoded_image_dir"]

with open(KEYS_FILE) as f:
    ALL_KEYS = json.load(f)

# ============ 解密函数 ============

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


# ============ DB 缓存 ============

class DBCache:
    """缓存解密后的 DB，通过 mtime 检测变化"""

    def __init__(self):
        self._cache = {}  # rel_key -> (db_mtime, wal_mtime, tmp_path)

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


_cache = DBCache()
atexit.register(_cache.cleanup)


# ============ 联系人缓存 ============

_contact_names = None  # {username: display_name}
_contact_full = None   # [{username, nick_name, remark}]


def _load_contacts_from(db_path):
    names = {}
    full = []
    conn = sqlite3.connect(db_path)
    try:
        for r in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            uname, nick, remark = r
            display = remark if remark else nick if nick else uname
            names[uname] = display
            full.append({'username': uname, 'nick_name': nick or '', 'remark': remark or ''})
    finally:
        conn.close()
    return names, full


def get_contact_names():
    global _contact_names, _contact_full
    if _contact_names is not None:
        return _contact_names

    # 优先用已解密的 contact.db
    pre_decrypted = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        try:
            _contact_names, _contact_full = _load_contacts_from(pre_decrypted)
            return _contact_names
        except Exception:
            pass

    # 实时解密
    path = _cache.get("contact\\contact.db")
    if path:
        try:
            _contact_names, _contact_full = _load_contacts_from(path)
            return _contact_names
        except Exception:
            pass

    return {}


def get_contact_full():
    global _contact_full
    if _contact_full is None:
        get_contact_names()
    return _contact_full or []


# ============ 辅助函数 ============

def format_msg_type(t):
    return {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }.get(t, f'type={t}')


def resolve_username(chat_name):
    """将聊天名/备注名/wxid 解析为 username"""
    names = get_contact_names()

    # 直接是 username
    if chat_name in names or chat_name.startswith('wxid_') or '@chatroom' in chat_name:
        return chat_name

    # 模糊匹配(优先精确包含)
    chat_lower = chat_name.lower()
    for uname, display in names.items():
        if chat_lower == display.lower():
            return uname
    for uname, display in names.items():
        if chat_lower in display.lower():
            return uname

    return None


_zstd_dctx = zstd.ZstdDecompressor()


def _decompress_content(content, ct):
    """解压 zstd 压缩的消息内容"""
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='replace')
        except Exception:
            return None
    return content


def _parse_message_content(content, local_type, is_group):
    """解析消息内容，返回 (sender_id, text)"""
    if content is None:
        return '', ''
    if isinstance(content, bytes):
        return '', '(二进制内容)'

    sender = ''
    text = content
    if is_group and ':\n' in content:
        sender, text = content.split(':\n', 1)

    return sender, text


# 消息 DB 的 rel_keys（排除 fts/resource/media/biz）
MSG_DB_KEYS = sorted([
    k for k in ALL_KEYS
    if k.startswith("message\\message_") and k.endswith(".db")
    and "fts" not in k and "resource" not in k
])


def _find_msg_table_for_user(username):
    """在所有 message_N.db 中查找用户的消息表，返回 (db_path, table_name)"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"

    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
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
            conn.close()

    return None, None


# ============ MCP Server ============

mcp = FastMCP("wechat", instructions="查询微信消息、联系人等数据")

# 新消息追踪
_last_check_state = {}  # {username: last_timestamp}


@mcp.tool()
def get_recent_sessions(limit: int = 20) -> str:
    """获取微信最近会话列表，包含最新消息摘要、未读数、时间等。
    用于了解最近有哪些人/群在聊天。

    Args:
        limit: 返回的会话数量，默认20
    """
    path = _cache.get("session\\session.db")
    if not path:
        return "错误: 无法解密 session.db"

    names = get_contact_names()
    conn = sqlite3.connect(path)
    rows = conn.execute("""
        SELECT username, unread_count, summary, last_timestamp,
               last_msg_type, last_msg_sender, last_sender_display_name
        FROM SessionTable
        WHERE last_timestamp > 0
        ORDER BY last_timestamp DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    results = []
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        display = names.get(username, username)
        is_group = '@chatroom' in username

        if isinstance(summary, bytes):
            try:
                summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
            except Exception:
                summary = '(压缩内容)'
        if isinstance(summary, str) and ':\n' in summary:
            summary = summary.split(':\n', 1)[1]

        sender_display = ''
        if is_group and sender:
            sender_display = names.get(sender, sender_name or sender)

        time_str = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')

        entry = f"[{time_str}] {display}"
        if is_group:
            entry += " [群]"
        if unread and unread > 0:
            entry += f" ({unread}条未读)"
        entry += f"\n  {format_msg_type(msg_type)}: "
        if sender_display:
            entry += f"{sender_display}: "
        entry += str(summary or "(无内容)")

        results.append(entry)

    return f"最近 {len(results)} 个会话:\n\n" + "\n\n".join(results)


@mcp.tool()
def get_chat_history(chat_name: str, limit: int = 50) -> str:
    """获取指定聊天的消息记录。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid，自动模糊匹配
        limit: 返回的消息数量，默认50
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}\n提示: 可以用 get_contacts(query='{chat_name}') 搜索联系人"

    names = get_contact_names()
    display_name = names.get(username, username)
    is_group = '@chatroom' in username

    db_path, table_name = _find_msg_table_for_user(username)
    if not db_path:
        return f"找不到 {display_name} 的消息记录（可能在未解密的DB中或无消息）"

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"""
            SELECT local_id, local_type, create_time, message_content,
                   WCDB_CT_message_content
            FROM [{table_name}]
            ORDER BY create_time DESC
            LIMIT ?
        """, (limit,)).fetchall()
    except Exception as e:
        conn.close()
        return f"查询失败: {e}"
    conn.close()

    if not rows:
        return f"{display_name} 无消息记录"

    lines = []
    for local_id, local_type, create_time, content, ct in reversed(rows):
        time_str = datetime.fromtimestamp(create_time).strftime('%m-%d %H:%M')

        # zstd 解压
        content = _decompress_content(content, ct)
        if content is None:
            content = '(无法解压)'

        sender, text = _parse_message_content(content, local_type, is_group)

        if local_type == 3:
            text = f"[图片] (local_id={local_id})"
        elif local_type == 47:
            text = "[表情]"
        elif local_type != 1:
            type_label = format_msg_type(local_type)
            text = f"[{type_label}] {text}" if text else f"[{type_label}]"

        if text and len(text) > 500:
            text = text[:500] + "..."

        if is_group and sender:
            sender_name = names.get(sender, sender)
            lines.append(f"[{time_str}] {sender_name}: {text}")
        else:
            lines.append(f"[{time_str}] {text}")

    header = f"{display_name} 的最近 {len(lines)} 条消息"
    if is_group:
        header += " [群聊]"
    return header + ":\n\n" + "\n".join(lines)


@mcp.tool()
def search_messages(keyword: str, limit: int = 20) -> str:
    """在所有聊天记录中搜索包含关键词的消息。

    Args:
        keyword: 搜索关键词
        limit: 返回的结果数量，默认20
    """
    if not keyword or len(keyword) < 1:
        return "请提供搜索关键词"

    names = get_contact_names()
    results = []

    for rel_key in MSG_DB_KEYS:
        if len(results) >= limit:
            break

        path = _cache.get(rel_key)
        if not path:
            continue

        conn = sqlite3.connect(path)
        try:
            # 获取所有 Msg_ 表
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()

            # 获取 Name2Id 映射（hash -> username 反查）
            name2id = {}
            try:
                for r in conn.execute("SELECT user_name FROM Name2Id").fetchall():
                    h = hashlib.md5(r[0].encode()).hexdigest()
                    name2id[f"Msg_{h}"] = r[0]
            except Exception:
                pass

            for (tname,) in tables:
                if len(results) >= limit:
                    break
                username = name2id.get(tname, '')
                is_group = '@chatroom' in username
                display = names.get(username, username) if username else tname

                try:
                    rows = conn.execute(f"""
                        SELECT local_type, create_time, message_content,
                               WCDB_CT_message_content
                        FROM [{tname}]
                        WHERE message_content LIKE ?
                        ORDER BY create_time DESC
                        LIMIT ?
                    """, (f'%{keyword}%', limit - len(results))).fetchall()
                except Exception:
                    continue

                for local_type, ts, content, ct in rows:
                    content = _decompress_content(content, ct)
                    if content is None:
                        continue
                    sender, text = _parse_message_content(content, local_type, is_group)
                    time_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                    sender_name = ''
                    if is_group and sender:
                        sender_name = names.get(sender, sender)

                    entry = f"[{time_str}] [{display}]"
                    if sender_name:
                        entry += f" {sender_name}:"
                    entry += f" {text}"
                    if len(entry) > 300:
                        entry = entry[:300] + "..."
                    results.append((ts, entry))
        finally:
            conn.close()

    results.sort(key=lambda x: x[0], reverse=True)
    entries = [r[1] for r in results[:limit]]

    if not entries:
        return f"未找到包含 \"{keyword}\" 的消息"

    return f"搜索 \"{keyword}\" 找到 {len(entries)} 条结果:\n\n" + "\n\n".join(entries)


@mcp.tool()
def get_contacts(query: str = "", limit: int = 50) -> str:
    """搜索或列出微信联系人。

    Args:
        query: 搜索关键词（匹配昵称、备注名、wxid），留空列出所有
        limit: 返回数量，默认50
    """
    contacts = get_contact_full()
    if not contacts:
        return "错误: 无法加载联系人数据"

    if query:
        q = query.lower()
        filtered = [
            c for c in contacts
            if q in c['nick_name'].lower()
            or q in c['remark'].lower()
            or q in c['username'].lower()
        ]
    else:
        filtered = contacts

    filtered = filtered[:limit]

    if not filtered:
        return f"未找到匹配 \"{query}\" 的联系人"

    lines = []
    for c in filtered:
        line = c['username']
        if c['remark']:
            line += f"  备注: {c['remark']}"
        if c['nick_name']:
            line += f"  昵称: {c['nick_name']}"
        lines.append(line)

    header = f"找到 {len(filtered)} 个联系人"
    if query:
        header += f"（搜索: {query}）"
    return header + ":\n\n" + "\n".join(lines)


@mcp.tool()
def get_new_messages() -> str:
    """获取自上次调用以来的新消息。首次调用返回最近的会话状态。"""
    global _last_check_state

    path = _cache.get("session\\session.db")
    if not path:
        return "错误: 无法解密 session.db"

    names = get_contact_names()
    conn = sqlite3.connect(path)
    rows = conn.execute("""
        SELECT username, unread_count, summary, last_timestamp,
               last_msg_type, last_msg_sender, last_sender_display_name
        FROM SessionTable
        WHERE last_timestamp > 0
        ORDER BY last_timestamp DESC
    """).fetchall()
    conn.close()

    curr_state = {}
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        curr_state[username] = {
            'unread': unread, 'summary': summary, 'timestamp': ts,
            'msg_type': msg_type, 'sender': sender or '', 'sender_name': sender_name or '',
        }

    if not _last_check_state:
        _last_check_state = {u: s['timestamp'] for u, s in curr_state.items()}
        # 首次调用，返回有未读的会话
        unread_msgs = []
        for username, s in curr_state.items():
            if s['unread'] and s['unread'] > 0:
                display = names.get(username, username)
                is_group = '@chatroom' in username
                summary = s['summary']
                if isinstance(summary, bytes):
                    try:
                        summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                    except Exception:
                        summary = '(压缩内容)'
                if isinstance(summary, str) and ':\n' in summary:
                    summary = summary.split(':\n', 1)[1]
                time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M')
                tag = "[群]" if is_group else ""
                unread_msgs.append(f"[{time_str}] {display}{tag} ({s['unread']}条未读): {summary}")

        if unread_msgs:
            return f"当前 {len(unread_msgs)} 个未读会话:\n\n" + "\n".join(unread_msgs)
        return "当前无未读消息（已记录状态，下次调用将返回新消息）"

    # 对比上次状态
    new_msgs = []
    for username, s in curr_state.items():
        prev_ts = _last_check_state.get(username, 0)
        if s['timestamp'] > prev_ts:
            display = names.get(username, username)
            is_group = '@chatroom' in username
            summary = s['summary']
            if isinstance(summary, bytes):
                try:
                    summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                except Exception:
                    summary = '(压缩内容)'
            if isinstance(summary, str) and ':\n' in summary:
                summary = summary.split(':\n', 1)[1]

            sender_display = ''
            if is_group and s['sender']:
                sender_display = names.get(s['sender'], s['sender_name'] or s['sender'])

            time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M:%S')
            entry = f"[{time_str}] {display}"
            if is_group:
                entry += " [群]"
            entry += f": {format_msg_type(s['msg_type'])}"
            if sender_display:
                entry += f" ({sender_display})"
            entry += f" - {summary}"
            new_msgs.append((s['timestamp'], entry))

    _last_check_state = {u: s['timestamp'] for u, s in curr_state.items()}

    if not new_msgs:
        return "无新消息"

    new_msgs.sort(key=lambda x: x[0])
    entries = [m[1] for m in new_msgs]
    return f"{len(entries)} 条新消息:\n\n" + "\n".join(entries)


# ============ 图片解密 ============

_image_resolver = ImageResolver(WECHAT_BASE_DIR, DECODED_IMAGE_DIR, _cache)


@mcp.tool()
def decode_image(chat_name: str, local_id: int) -> str:
    """解密微信聊天中的一张图片。

    先用 get_chat_history 查看消息，图片消息会显示 local_id，
    然后用此工具解密对应图片。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 图片消息的 local_id（从 get_chat_history 获取）
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    result = _image_resolver.decode_image(username, local_id)
    if result['success']:
        return (
            f"解密成功!\n"
            f"  文件: {result['path']}\n"
            f"  格式: {result['format']}\n"
            f"  大小: {result['size']:,} bytes\n"
            f"  MD5: {result['md5']}"
        )
    else:
        error = result['error']
        if 'md5' in result:
            error += f"\n  MD5: {result['md5']}"
        return f"解密失败: {error}"


@mcp.tool()
def get_chat_images(chat_name: str, limit: int = 20) -> str:
    """列出某个聊天中的图片消息。

    返回图片的时间、local_id、MD5、文件大小等信息。
    可以配合 decode_image 工具解密指定图片。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        limit: 返回数量，默认20
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    names = get_contact_names()
    display_name = names.get(username, username)

    db_path, table_name = _find_msg_table_for_user(username)
    if not db_path:
        return f"找不到 {display_name} 的消息记录"

    images = _image_resolver.list_chat_images(db_path, table_name, username, limit)
    if not images:
        return f"{display_name} 无图片消息"

    lines = []
    for img in images:
        time_str = datetime.fromtimestamp(img['create_time']).strftime('%Y-%m-%d %H:%M')
        line = f"[{time_str}] local_id={img['local_id']}"
        if img.get('md5'):
            line += f"  MD5={img['md5']}"
        if img.get('size'):
            size_kb = img['size'] / 1024
            line += f"  {size_kb:.0f}KB"
        if not img.get('md5'):
            line += "  (无资源信息)"
        lines.append(line)

    return f"{display_name} 的 {len(lines)} 张图片:\n\n" + "\n".join(lines)


# ============ 实时消息监听 (Live Feed) ============

_LIVE_FEED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "live", "incoming.jsonl"
)
_LIVE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "live", "monitor_state.json"
)
_live_feed_offset = 0  # 已读到的文件行偏移


@mcp.tool()
def get_live_feed(chat_name: str = "", mark_read: bool = True) -> str:
    """读取 live_monitor.py 监听到的最新消息，用于实时对话提示。

    用法：
    1. 先让用户在后台运行: python tools/live_monitor.py --name "谢雨欣"
    2. 当用户问"有什么新消息"时，调用此工具
    3. 返回自上次读取以来的新消息（包含已解码的语音文字）

    Args:
        chat_name: 可选，过滤特定联系人的消息（留空返回所有）
        mark_read: 是否标记为已读（默认True，下次只返回更新的消息）

    Returns:
        新消息列表，包含时间、发送者、类型、内容、语音转录等
    """
    global _live_feed_offset

    if not os.path.exists(_LIVE_FEED_FILE):
        # 检查 monitor 是否在运行
        monitor_status = ""
        if os.path.exists(_LIVE_STATE_FILE):
            try:
                with open(_LIVE_STATE_FILE, 'r') as f:
                    state = json.load(f)
                monitor_status = "\n监听状态文件存在，但尚未收到新消息。"
            except Exception:
                monitor_status = ""
        return f"暂无实时消息。请确认 live_monitor.py 正在运行。{monitor_status}"

    try:
        with open(_LIVE_FEED_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取实时消息文件失败: {e}"

    total_lines = len(lines)

    if _live_feed_offset >= total_lines:
        return "无新消息"

    # 读取新行
    new_lines = lines[_live_feed_offset:]

    if mark_read:
        _live_feed_offset = total_lines

    # 解析并格式化
    messages = []
    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        # 过滤
        if chat_name:
            target = chat_name.lower()
            if target not in msg.get('sender', '').lower():
                continue

        time_str = msg.get('time', '?')
        sender = msg.get('sender', '?')
        type_name = msg.get('type_name', '?')

        if msg.get('type') == 1:  # 文本
            body = msg.get('content', '')
        elif msg.get('type') == 34:  # 语音
            body = f"[语音]"
            if msg.get('voice_text'):
                body += f" {msg['voice_text']}"
        elif msg.get('type') == 47:  # 表情
            body = "[表情]"
        elif msg.get('type') == 3:  # 图片
            body = "[图片]"
        else:
            body = f"[{type_name}]"
            if msg.get('content'):
                body += f" {msg['content'][:100]}"

        messages.append({
            "time": time_str,
            "sender": sender,
            "type": type_name,
            "body": body,
        })

    if not messages:
        if mark_read:
            _live_feed_offset = total_lines
        return "无新消息"

    # 格式化输出
    lines_out = []
    for m in messages:
        line = f"[{m['time']}] {m['sender']} | {m['type']}"
        body = m['body']
        if m['type'] != '文本' and m['type'] != '语音':
            line += f": {body}"
            lines_out.append(line)
        else:
            lines_out.append(line)
            lines_out.append(f"  {body}")

    summary = f"{len(messages)} 条新消息:\n\n" + "\n".join(lines_out)

    return summary


@mcp.tool()
def get_live_status() -> str:
    """检查 live_monitor.py 的运行状态"""
    status_parts = []

    # 检查状态文件
    if os.path.exists(_LIVE_STATE_FILE):
        try:
            with open(_LIVE_STATE_FILE, 'r') as f:
                state = json.load(f)
            status_parts.append("✅ 状态文件存在")
            for key, val in state.items():
                if key.startswith("last_ts_"):
                    name = key.replace("last_ts_", "")
                    ts = datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S") if val else "首次运行"
                    status_parts.append(f"  {name}: 上次检查 = {ts}")
        except Exception as e:
            status_parts.append(f"⚠ 状态文件读取失败: {e}")
    else:
        status_parts.append("❌ 状态文件不存在 — live_monitor.py 可能未运行")

    # 检查 feed 文件
    if os.path.exists(_LIVE_FEED_FILE):
        try:
            with open(_LIVE_FEED_FILE, 'r', encoding='utf-8') as f:
                line_count = sum(1 for _ in f)
            status_parts.append(f"📨 消息文件: {line_count} 条已记录")
        except Exception:
            pass
    else:
        status_parts.append("📨 消息文件未创建")

    status_parts.append(f"\n使用方式: python tools/live_monitor.py --name '联系人名'")

    return "\n".join(status_parts)


if __name__ == "__main__":
    mcp.run()
