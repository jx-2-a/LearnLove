"""
从解密后的微信数据库提取指定联系人的聊天记录

用法:
    source .venv/Scripts/activate
    python tools/extract_chat.py --name "ta的名字" --days 30
    python tools/extract_chat.py --list           # 列出所有会话
    python tools/extract_chat.py --name "xxx"     # 默认最近7天
"""

import os, sys, json, hashlib, sqlite3, argparse
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECRYPTED_DIR = os.path.join(PROJECT_ROOT, "wechat-decrypt", "decrypted")

import zstandard as zstd
_zstd_dctx = zstd.ZstdDecompressor()


def load_name2id(db_path):
    """加载 username -> 会话映射"""
    mapping = {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT user_name, is_session FROM Name2Id").fetchall()
        for user_name, is_session in rows:
            if is_session:
                mapping[user_name] = True
    finally:
        conn.close()
    return mapping


def load_contacts(db_path):
    """加载联系人信息: username -> {remark, nick_name}"""
    contacts = {}
    conn = sqlite3.connect(db_path)
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(contact)").fetchall()]
        rows = conn.execute("SELECT * FROM contact").fetchall()
        for row in rows:
            d = dict(zip(cols, row))
            username = d.get("username", "")
            contacts[username] = {
                "remark": d.get("remark", "") or "",
                "nick_name": d.get("nick_name", "") or "",
            }
    finally:
        conn.close()
    return contacts


def find_chat_table(db_dir, username):
    """在 message_N.db 中查找用户的聊天表"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"

    for f in sorted(os.listdir(os.path.join(db_dir, "message"))):
        if not f.startswith("message_") or not f.endswith(".db"):
            continue
        if "fts" in f or "resource" in f or "biz" in f:
            continue

        path = os.path.join(db_dir, "message", f)
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if exists:
                conn.close()
                return path, table_name
        finally:
            try:
                conn.close()
            except:
                pass
    return None, None


def decompress(content, ct):
    """ZSTD 解压消息内容"""
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='replace')
        except:
            return None
    return content


def parse_message(content, local_type, username):
    """解析消息，提取发送者和文本"""
    if content is None:
        return "", "(空)"

    is_group = "@chatroom" in username
    sender = ""
    text = content

    if is_group and ":\n" in content:
        parts = content.split(":\n", 1)
        sender = parts[0]
        text = parts[1] if len(parts) > 1 else content

    return sender, text


MSG_TYPES = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/文件", 50: "VOIP",
    10000: "系统消息", 10002: "系统消息",
    244813135921: "引用",
    25769803825: "文件",
}


def extract_messages(db_dir, username, days=7):
    """提取指定用户最近 N 天的消息"""
    db_path, table_name = find_chat_table(db_dir, username)
    if not db_path:
        return None, f"未找到用户: {username}"

    cutoff = int((datetime.now() - timedelta(days=days)).timestamp())

    conn = sqlite3.connect(db_path)
    messages = []

    try:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
        col_map = {name: i for i, name in enumerate(cols)}

        rows = conn.execute(
            f"SELECT * FROM {table_name} WHERE create_time >= ? ORDER BY create_time ASC",
            (cutoff,)
        ).fetchall()

        for row in rows:
            d = dict(zip(cols, row))
            content = decompress(
                d.get("message_content"),
                d.get("WCDB_CT_message_content")
            )
            sender, text = parse_message(content, d.get("local_type", 0), username)
            msg_type = d.get("local_type", 0)

            messages.append({
                "time": datetime.fromtimestamp(d["create_time"]).strftime("%Y-%m-%d %H:%M"),
                "sender_id": d.get("real_sender_id", 0),
                "sender": sender,
                "type": msg_type,
                "type_name": MSG_TYPES.get(msg_type, f"未知({msg_type})"),
                "content": text,
            })

    finally:
        conn.close()

    return messages, None


def list_sessions(db_dir):
    """列出所有活跃会话（带备注名和最近消息）"""
    session_db = os.path.join(db_dir, "session", "session.db")
    contact_db = os.path.join(db_dir, "contact", "contact.db")

    contacts = load_contacts(contact_db)

    sessions = []
    conn = sqlite3.connect(session_db)
    try:
        rows = conn.execute(
            "SELECT username, summary, last_timestamp FROM SessionTable "
            "WHERE username != 'brandsessionholder' ORDER BY sort_timestamp DESC LIMIT 50"
        ).fetchall()

        for username, summary, ts in rows:
            if not username:
                continue
            contact = contacts.get(username, {})
            display = contact.get("remark") or contact.get("nick_name") or username

            # 解压摘要
            if isinstance(summary, bytes):
                try:
                    summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                except:
                    summary = str(summary)[:80]

            sessions.append({
                "username": username,
                "display_name": display,
                "is_group": "@chatroom" in username,
                "last_time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "",
                "summary": str(summary)[:80] if summary else "",
            })
    finally:
        conn.close()

    return sessions


def safe_print(*args, **kwargs):
    """安全打印，处理 Windows GBK 编码问题"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        print(*(str(a).encode('ascii', errors='replace').decode('ascii') for a in args), **kwargs)


def main():
    parser = argparse.ArgumentParser(description="微信聊天记录提取工具")
    parser.add_argument("--name", help="联系人名称（支持模糊匹配备注/昵称/username）")
    parser.add_argument("--list", action="store_true", help="列出所有会话")
    parser.add_argument("--days", type=int, default=7, help="提取最近N天 (默认7)")
    parser.add_argument("--output", default="data/chats", help="输出目录")
    args = parser.parse_args()

    db_dir = DECRYPTED_DIR

    if args.list:
        print("=" * 60)
        print("  微信会话列表")
        print("=" * 60)
        sessions = list_sessions(db_dir)
        for i, s in enumerate(sessions):
            flag = "[群]" if s["is_group"] else "[人]"
            print(f"{i+1:>3}. {flag} {s['display_name']}")
            print(f"     {s['username']}")
            print(f"     最后: {s['last_time']} | {s['summary'][:50]}")
            print()
        return

    if not args.name:
        parser.print_help()
        return

    # 模糊搜索匹配
    contacts = load_contacts(os.path.join(db_dir, "contact", "contact.db"))
    sessions = list_sessions(db_dir)

    matched = None
    query = args.name.lower()
    for s in sessions:
        contact = contacts.get(s["username"], {})
        candidates = [
            s["username"].lower(),
            s["display_name"].lower(),
            contact.get("remark", "").lower(),
            contact.get("nick_name", "").lower(),
        ]
        if any(query in c for c in candidates if c):
            matched = s["username"]
            print(f"[+] 匹配到: {s['display_name']} ({s['username']})")
            break

    if not matched:
        print(f"[!] 未找到匹配 '{args.name}' 的联系人，用 --list 查看所有会话")
        return

    print(f"[+] 提取最近 {args.days} 天的消息...")
    messages, error = extract_messages(db_dir, matched, args.days)

    if error:
        print(f"[!] {error}")
        return

    print(f"[OK] 共 {len(messages)} 条消息\n")

    # 输出
    out_path = args.output
    os.makedirs(out_path, exist_ok=True)

    # JSON 输出
    safe_name = "".join(c if c.isalnum() else "_" for c in args.name)
    json_path = os.path.join(out_path, f"chat_{safe_name}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

    # 文本预览
    print("=" * 60)
    print(f"  {args.name} — 最近 {args.days} 天")
    print("=" * 60)
    for m in messages[-50:]:
        prefix = f"[{m['time']}]"
        if m['sender']:
            prefix += f" {m['sender']}"
        if m['type_name'] != '文本':
            prefix += f" [{m['type_name']}]"
        print(f"{prefix}")
        if m['content']:
            print(f"  {m['content'][:200]}")
        print()

    print(f"\n[OK] 完整数据保存到: {json_path}")
    print(f"[OK] 共 {len(messages)} 条消息")


if __name__ == '__main__':
    main()
