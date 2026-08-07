"""
批量语音转文字：从 VoiceInfo 提取 SILK → pilk 解码 → faster-whisper 转录
支持增量缓存，断点续传

用法:
    python tools/transcribe_voice.py --name "谢雨欣" --model small
    python tools/transcribe_voice.py --name "谢雨欣" --model tiny   # 快速测试
    python tools/transcribe_voice.py --list-chats                    # 列出所有有语音的会话
"""
import sqlite3, os, sys, json, hashlib, argparse, tempfile, wave, time
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DB = os.path.join(PROJECT_ROOT, "wechat-decrypt", "decrypted", "message", "media_0.db")
MESSAGE_DIR = os.path.join(PROJECT_ROOT, "wechat-decrypt", "decrypted", "message")
CONTACT_DB = os.path.join(PROJECT_ROOT, "wechat-decrypt", "decrypted", "contact", "contact.db")
SESSION_DB = os.path.join(PROJECT_ROOT, "wechat-decrypt", "decrypted", "session", "session.db")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "voice_transcripts")
CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "voice_cache")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)


def resolve_chat_name_id(username):
    """通过时间匹配找到 VoiceInfo 中对应的 chat_name_id
    方法：用 Msg 表 type=34 的 create_time 与 VoiceInfo 交叉匹配
    """
    db_path, table_name = find_chat_table(username)
    if not db_path:
        return None

    msg_conn = sqlite3.connect(db_path)
    # 获取该用户最近几条语音的 create_time
    voice_times = msg_conn.execute(
        f"SELECT create_time FROM {table_name} WHERE local_type=34 ORDER BY create_time DESC LIMIT 100"
    ).fetchall()
    msg_conn.close()

    if not voice_times:
        return None

    times = [t[0] for t in voice_times]

    media_conn = sqlite3.connect(MEDIA_DB)
    # 找到包含这些 create_time 的 chat_name_id
    for chat_id in [1, 2, 3, 4, 5]:
        matches = media_conn.execute(
            "SELECT COUNT(*) FROM VoiceInfo WHERE chat_name_id=? AND create_time IN ({})".format(
                ','.join('?' * len(times))
            ),
            [chat_id] + times
        ).fetchone()[0]
        if matches > len(times) * 0.3:  # 30%+ 匹配
            media_conn.close()
            return chat_id
    media_conn.close()
    return None


def load_contacts():
    """加载联系人信息"""
    contacts = {}
    conn = sqlite3.connect(CONTACT_DB)
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


def find_chat_table(username):
    """找到用户的 Msg_ 表"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"

    for f in sorted(os.listdir(MESSAGE_DIR)):
        if not f.startswith("message_") or "fts" in f or "resource" in f or "biz" in f:
            continue
        path = os.path.join(MESSAGE_DIR, f)
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


def get_voice_messages(chat_name_id, username):
    """获取指定 chat 的所有语音消息，关联 VoiceInfo 和 Msg_ 表"""
    media_conn = sqlite3.connect(MEDIA_DB)

    # 获取语音数据
    voice_rows = media_conn.execute(
        "SELECT chat_name_id, create_time, local_id, svr_id, voice_data, length(voice_data) "
        "FROM VoiceInfo WHERE voice_data IS NOT NULL AND chat_name_id=? "
        "ORDER BY create_time ASC",
        (chat_name_id,)
    ).fetchall()
    media_conn.close()

    if not voice_rows:
        return []

    # 获取消息元数据
    db_path, table_name = find_chat_table(username)
    if not db_path:
        print(f"  [WARN] 未找到 {username} 的消息表，仅返回语音数据")
        return [
            {
                "chat_id": chat_name_id,
                "create_time": ts,
                "local_id": local_id,
                "svr_id": svr_id,
                "voice_data": data,
                "data_len": data_len,
                "sender_id": 0,
                "sender_name": "",
            }
            for chat_id, ts, local_id, svr_id, data, data_len in voice_rows
        ]

    msg_conn = sqlite3.connect(db_path)
    # 获取消息元数据 (type=34 是语音)
    # 也查 message_1.db 等旧数据库
    all_msg_conns = [msg_conn]
    for f in sorted(os.listdir(MESSAGE_DIR)):
        if not f.startswith("message_") or "fts" in f or "resource" in f or "biz" in f:
            continue
        if f == os.path.basename(db_path):
            continue
        path = os.path.join(MESSAGE_DIR, f)
        conn2 = sqlite3.connect(path)
        exists = conn2.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        if exists:
            all_msg_conns.append(conn2)
        else:
            conn2.close()

    msg_rows = []
    for conn in all_msg_conns:
        rows = conn.execute(
            f"SELECT create_time, real_sender_id, local_type FROM {table_name} "
            f"WHERE local_type=34 AND create_time >= ? AND create_time <= ? "
            f"ORDER BY create_time ASC",
            (voice_rows[0][1] - 10, voice_rows[-1][1] + 10)
        ).fetchall()
        msg_rows.extend(rows)

    for conn in all_msg_conns:
        try:
            conn.close()
        except:
            pass

    # 按时间匹配
    msg_by_time = {}
    for ts, sender_id, lt in msg_rows:
        msg_by_time[ts] = (sender_id, lt)

    # 构建消息列表
    messages = []
    for chat_id, ts, local_id, svr_id, data, data_len in voice_rows:
        meta = msg_by_time.get(ts, (0, 34))
        messages.append({
            "chat_id": chat_id,
            "create_time": ts,
            "local_id": local_id,
            "svr_id": svr_id,
            "voice_data": data,
            "data_len": data_len,
            "sender_id": meta[0],
        })

    return messages


def get_cache_key(voice_data):
    """基于语音数据生成缓存键"""
    return hashlib.md5(voice_data).hexdigest()


def transcribe_voice(voice_data, model, cache_key):
    """解码 SILK 并转录，带缓存"""
    cache_path = os.path.join(CACHE_DIR, f"{cache_key}.json")

    # 检查缓存
    if os.path.exists(cache_path):
        with open(cache_path, 'r', encoding='utf-8') as f:
            cached = json.load(f)
        return cached.get("text"), cached.get("duration_ms"), True

    # 写入临时 silk 文件
    silk_path = os.path.join(CACHE_DIR, f"{cache_key}.silk")
    pcm_path = os.path.join(CACHE_DIR, f"{cache_key}.pcm")

    try:
        with open(silk_path, 'wb') as f:
            f.write(voice_data)

        from pilk import decode, get_duration
        duration = get_duration(silk_path)
        decode(silk_path, pcm_path)

        # 读取 PCM 写入 WAV
        wav_path = os.path.join(CACHE_DIR, f"{cache_key}.wav")
        with open(pcm_path, 'rb') as f:
            pcm_data = f.read()

        import wave
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm_data)

        # faster-whisper 转录
        segments, info = model.transcribe(wav_path, language="zh", beam_size=5)
        text = "".join(seg.text for seg in segments)

        # 保存缓存
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump({"text": text, "duration_ms": duration}, f, ensure_ascii=False)

        return text, duration, False

    except Exception as e:
        print(f"  [ERROR] 转录失败: {e}")
        return None, None, False
    finally:
        # 清理临时文件
        for p in [silk_path, pcm_path]:
            if os.path.exists(p):
                os.unlink(p)
        wav = os.path.join(CACHE_DIR, f"{cache_key}.wav")
        if os.path.exists(wav):
            os.unlink(wav)


def list_chats_with_voice():
    """列出所有有语音的会话"""
    name2id = load_name2id()
    contacts = load_contacts()

    conn = sqlite3.connect(MEDIA_DB)
    rows = conn.execute(
        "SELECT chat_name_id, COUNT(*) as cnt, "
        "SUM(length(voice_data)) as total_bytes "
        "FROM VoiceInfo WHERE voice_data IS NOT NULL "
        "GROUP BY chat_name_id ORDER BY cnt DESC"
    ).fetchall()
    conn.close()

    for chat_id, cnt, total_bytes in rows:
        username = name2id.get(chat_id, f"未知会话{chat_id}")
        contact = contacts.get(username, {})
        display = contact.get("remark") or contact.get("nick_name") or username
        print(f"  [{chat_id}] {display} ({username}): {cnt} 条语音, {total_bytes/1024:.0f}KB")


def main():
    parser = argparse.ArgumentParser(description="批量语音转文字")
    parser.add_argument("--name", help="联系人名称（模糊匹配）")
    parser.add_argument("--model", default="small", choices=["tiny", "small", "medium"],
                        help="Whisper 模型大小 (默认 small)")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条数 (0=全部)")
    parser.add_argument("--list-chats", action="store_true", help="列出有语音的会话")
    parser.add_argument("--dry-run", action="store_true", help="只统计不转录")
    args = parser.parse_args()

    if args.list_chats:
        print("=== 有语音消息的会话 ===")
        list_chats_with_voice()
        return

    if not args.name:
        parser.print_help()
        return

    # 加载映射
    contacts = load_contacts()

    # 模糊匹配 username
    query = args.name.lower()
    matched_username = None
    matched_display = None

    for username in contacts:
        contact = contacts[username]
        candidates = [
            username.lower(),
            contact.get("remark", "").lower(),
            contact.get("nick_name", "").lower(),
        ]
        if any(query in c for c in candidates if c):
            matched_username = username
            matched_display = contact.get("remark") or contact.get("nick_name") or username
            break

    if not matched_username:
        # 也检查 SessionTable
        sess_conn = sqlite3.connect(SESSION_DB)
        sessions = sess_conn.execute(
            "SELECT username FROM SessionTable WHERE username != 'brandsessionholder'"
        ).fetchall()
        sess_conn.close()
        for (uname,) in sessions:
            contact = contacts.get(uname, {})
            candidates = [
                uname.lower(),
                contact.get("remark", "").lower(),
                contact.get("nick_name", "").lower(),
            ]
            if any(query in c for c in candidates if c):
                matched_username = uname
                matched_display = contact.get("remark") or contact.get("nick_name") or uname
                break

    if not matched_username:
        print(f"[!] 未找到匹配 '{args.name}' 的联系人")
        return

    # 通过时间匹配找到 chat_name_id
    matched_id = resolve_chat_name_id(matched_username)
    if not matched_id:
        print(f"[!] 无法确定 {matched_display} 的 VoiceInfo chat_name_id")
        print("[+] 可能该会话无语音消息，可用会话:")
        list_chats_with_voice()
        return

    print(f"[+] 匹配到: {matched_display} ({matched_username})")
    print(f"[+] chat_name_id={matched_id}")

    # 获取语音消息
    messages = get_voice_messages(matched_id, matched_username)
    print(f"[+] 共 {len(messages)} 条语音消息")

    if args.dry_run:
        # 统计
        total_dur = 0
        for m in messages:
            silk_path = os.path.join(CACHE_DIR, f"{get_cache_key(m['voice_data'])}.silk")
            with open(silk_path, 'wb') as f:
                f.write(m['voice_data'])
            from pilk import get_duration
            dur = get_duration(silk_path)
            total_dur += dur
            os.unlink(silk_path)
        print(f"[+] 预估总时长: {total_dur/1000:.0f}秒 ({total_dur/60000:.1f}分钟)")
        return

    if args.limit > 0:
        messages = messages[:args.limit]
        print(f"[+] 限制处理: {args.limit} 条")

    # 加载 Whisper 模型
    print(f"[+] 加载 Whisper 模型: {args.model}")
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(args.model, device="cuda", compute_type="float16")
    print(f"[+] 模型加载完成 ({time.time()-t0:.1f}s)")

    # 批量转录
    results = []
    n_cached = 0
    n_total = len(messages)
    total_transcribe_time = 0

    for i, m in enumerate(messages):
        ts_str = datetime.fromtimestamp(m["create_time"]).strftime("%Y-%m-%d %H:%M")
        cache_key = get_cache_key(m["voice_data"])

        print(f"\r[{i+1}/{n_total}] {ts_str} ...", end="", flush=True)

        t0 = time.time()
        text, duration, from_cache = transcribe_voice(m["voice_data"], model, cache_key)
        elapsed = time.time() - t0

        if not from_cache:
            total_transcribe_time += elapsed

        if from_cache:
            n_cached += 1

        sender = "谢雨欣" if m["sender_id"] == 1 else ("你" if m["sender_id"] == 2 else f"ID-{m['sender_id']}")

        results.append({
            "time": ts_str,
            "sender": sender,
            "sender_id": m["sender_id"],
            "local_id": m["local_id"],
            "svr_id": m["svr_id"],
            "duration_ms": duration,
            "text": text or "",
        })

    print(f"\n[OK] 转录完成! 共 {n_total} 条 (缓存命中 {n_cached} 条)")

    if n_total > n_cached:
        print(f"[+] 转录耗时: {total_transcribe_time:.1f}s (平均 {total_transcribe_time/(n_total-n_cached):.1f}s/条)")

    # 保存结果
    safe_name = "".join(c if c.isalnum() else "_" for c in args.name)
    output_path = os.path.join(OUTPUT_DIR, f"voice_{safe_name}_{args.model}.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[+] 结果保存到: {output_path}")

    # 预览
    print(f"\n=== 转录预览 (最近10条) ===")
    for r in results[-10:]:
        print(f"[{r['time']}] [{r['sender']}] ({r['duration_ms']:.0f}ms)")
        if r['text']:
            print(f"  {r['text']}")
        else:
            print(f"  (转录失败)")
        print()


if __name__ == "__main__":
    main()
