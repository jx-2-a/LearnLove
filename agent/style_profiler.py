"""
风格学习模块 — LLM 驱动的语言风格分析

功能：
  - 学习用户（你）的语言风格，让 Agent 回复更像你
  - 分析对方（联系人）的风格，让回复更对路

输出：
  - 用户风格: data/agent/styles/user_style.md
  - 联系人风格: data/agent/memory/<name>/style.md

分析维度：
  句式、语气、表情/符号、常用词汇、消息长度、互动模式
"""

import os
import json
from datetime import datetime

from agent.paths import (
    user_style_path as _user_style_path,
    user_style_meta_path as _user_style_meta_path,
    contact_style_path as _contact_style_path,
    styles_dir as _styles_dir,
    memory_dir as _memory_dir,
)

STYLE_ANALYSIS_PROMPT = """你是语言风格分析师。分析以下聊天消息样本，提取说话人的语言风格特征。

## 分析维度
1. **句式特征**: 短句/长句？喜欢分段还是一次发完？用不用标点？
2. **语气基调**: 活泼/温柔/幽默/直接/冷静/撒娇/调侃？
3. **表情符号**: 常用哪些表情/emoji？频率如何？
4. **口头禅**: 高频出现的词或短语（如"哈哈哈"、"可以的"、"太强了"）
5. **消息长度**: 通常发多长？一条说完还是多条？
6. **互动模式**: 主动提问多还是回应多？用不用反问？会不会自嘲？
7. **特色表达**: 独特的说话方式（如喜欢用比喻、喜欢用网络梗、语气词多等）

## 输出格式
直接输出 Markdown，固定结构：

### 句式
（2-3句描述）

### 语气
（2-3句描述）

### 表情符号
（列出常用的）

### 口头禅
（列出3-8个）

### 消息特征
（长度、分段习惯等）

### 互动风格
（2-3句描述）

### 给对方（AI）的建议
（基于以上分析，3-5条具体的回复风格建议，用"你"称呼AI）

总长度不超过 {max_chars} 字符。"""


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _messages_to_text(messages: list[dict], speaker_label: str) -> str:
    """将消息列表转为可分析的文本"""
    lines = []
    for m in messages:
        sender = m.get("sender", "?")
        content = m.get("content", "")
        time_str = m.get("time", "")
        # 只取指定说话人的消息
        if speaker_label == "user":
            if sender == "你":
                continue  # 跳过用户自己（在 sender_map 中映射为"你"的）
            # 用户发的是 sender != "你" 的情况比较复杂
            # 这里简化：如果传入了 user_messages，就用 raw_content
        if content and content.strip():
            lines.append(f"[{time_str}] {sender}: {content}")
    return "\n".join(lines)


def _run_style_analysis(messages_text: str, llm_config: dict, max_chars: int = 800) -> str:
    """调用 LLM 分析风格"""
    from agent.llm import chat_simple
    prompt = STYLE_ANALYSIS_PROMPT.replace("{max_chars}", str(max_chars))
    user_msg = f"## 消息样本\n{messages_text}\n\n请分析说话人的语言风格。"
    try:
        result = chat_simple(user_message=user_msg, system_prompt=prompt, config=llm_config)
        if result and not result.startswith("[LLM 错误]"):
            return result.strip()
    except Exception:
        pass
    return ""


# ===== 用户风格 =====

def get_user_style_path() -> str:
    return _user_style_path()


def get_user_style_meta_path() -> str:
    return _user_style_meta_path()


def load_user_style() -> str:
    """加载用户风格描述"""
    path = get_user_style_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def analyze_my_style(llm_config: dict = None) -> dict:
    """从最近的聊天记录中分析用户（你）的语言风格。

    采样所有监听联系人的最近聊天，提取用户发送的消息进行分析。
    """
    from agent.tools._state import state
    from agent.tools.message import _find_msg_table, _query_messages, _decode_text, _get_sender_map
    import sqlite3

    if not llm_config:
        llm_config = state.config.get("llm", {})

    # 从所有配置的联系人中采样用户消息
    contacts_cfg = state.contacts_config()
    all_user_msgs = []

    for c in contacts_cfg:
        wxid = c.get("wxid", "")
        display = c.get("name", "")
        tables = _find_msg_table(wxid)
        for db_path, table_name in tables:
            try:
                conn = sqlite3.connect(db_path)
                smap = _get_sender_map(conn, table_name, wxid, display)
                msgs = _query_messages(conn, table_name, limit=100)
                conn.close()

                # 找出用户自己的消息（sender_id 对应"你"的）
                user_sid = None
                for sid, name in smap.items():
                    if name == "你":
                        user_sid = sid
                        break

                for m in msgs:
                    if m["sender_id"] == user_sid:
                        text = m["content"]
                        if text and len(text) > 2 and text[0] != "[":
                            from datetime import datetime as dt
                            ts_str = dt.fromtimestamp(m["create_time"]).strftime("%m-%d %H:%M")
                            all_user_msgs.append({
                                "time": ts_str,
                                "content": text,
                            })
            except Exception:
                continue

    if not all_user_msgs:
        return {"ok": False, "error": "没有找到足够的用户消息（至少需要一些聊天记录）"}

    # 取最近 80 条
    all_user_msgs.sort(key=lambda m: m["time"])
    sample = all_user_msgs[-80:]

    # 构建分析文本
    lines = [f"[{m['time']}] 你: {m['content']}" for m in sample]
    text = "\n".join(lines)

    # LLM 分析
    result = _run_style_analysis(text, llm_config)
    if not result:
        return {"ok": False, "error": "LLM 风格分析失败"}

    # 保存
    header = f"<!-- analyzed: {datetime.now().isoformat()} messages: {len(sample)} -->\n\n"
    style_path = get_user_style_path()
    os.makedirs(os.path.dirname(style_path), exist_ok=True)
    with open(style_path, "w", encoding="utf-8") as f:
        f.write(header + result)

    # 保存元信息
    _save_json(get_user_style_meta_path(), {
        "last_analyzed": datetime.now().isoformat(),
        "message_count": len(sample),
        "contacts_sampled": [c.get("name", "") for c in contacts_cfg],
    })

    return {"ok": True, "data": {
        "style": result,
        "message_count": len(sample),
        "path": style_path,
    }}


# ===== 联系人风格 =====

def get_contact_style_path(contact_name: str) -> str:
    return _contact_style_path(contact_name)


def load_contact_style(contact_name: str) -> str:
    """加载某联系人的风格描述"""
    path = get_contact_style_path(contact_name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def analyze_contact_style(contact_name: str, llm_config: dict = None) -> dict:
    """分析某联系人的语言风格"""
    from agent.tools._state import state
    from agent.tools.message import _find_msg_table, _query_messages, _get_sender_map
    import sqlite3

    if not llm_config:
        llm_config = state.config.get("llm", {})

    wxid, display = state.resolve_contact_exact(contact_name)
    if not wxid:
        return {"ok": False, "error": f"未找到联系人: {contact_name}"}

    tables = _find_msg_table(wxid)
    if not tables:
        return {"ok": False, "error": f"未找到 {display} 的消息表"}

    all_msgs = []
    for db_path, table_name in tables:
        try:
            conn = sqlite3.connect(db_path)
            smap = _get_sender_map(conn, table_name, wxid, display)
            msgs = _query_messages(conn, table_name, limit=200)
            conn.close()

            # 找出对方的消息（sender_id 对应 display_name 的）
            contact_sid = None
            for sid, name in smap.items():
                if name == display:
                    contact_sid = sid
                    break

            for m in msgs:
                if m["sender_id"] == contact_sid:
                    text = m["content"]
                    if text and len(text) > 2 and text[0] != "[":
                        from datetime import datetime as dt
                        ts_str = dt.fromtimestamp(m["create_time"]).strftime("%m-%d %H:%M")
                        all_msgs.append({
                            "time": ts_str,
                            "content": text,
                        })
        except Exception:
            continue

    if not all_msgs:
        return {"ok": False, "error": f"没有找到 {display} 的足够消息"}

    all_msgs.sort(key=lambda m: m["time"])
    sample = all_msgs[-100:]

    lines = [f"[{m['time']}] {display}: {m['content']}" for m in sample]
    text = "\n".join(lines)

    result = _run_style_analysis(text, llm_config)
    if not result:
        return {"ok": False, "error": "LLM 风格分析失败"}

    # 保存
    header = f"<!-- analyzed: {datetime.now().isoformat()} messages: {len(sample)} -->\n\n"
    style_path = get_contact_style_path(display)
    os.makedirs(os.path.dirname(style_path), exist_ok=True)
    with open(style_path, "w", encoding="utf-8") as f:
        f.write(header + result)

    return {"ok": True, "data": {
        "contact": display,
        "style": result,
        "message_count": len(sample),
    }}


def view_style(contact_name: str = "") -> dict:
    """查看风格分析结果。不指定则查看用户自己的风格。"""
    if contact_name:
        style = load_contact_style(contact_name)
        if style:
            return {"ok": True, "data": {"contact": contact_name, "style": style}}
        return {"ok": True, "data": {"contact": contact_name, "style": "(尚未分析)", "hint": "使用 analyze_contact_style 分析"}}
    else:
        style = load_user_style()
        if style:
            return {"ok": True, "data": {"style": style}}
        return {"ok": True, "data": {"style": "(尚未分析)", "hint": "使用 analyze_my_style 分析你自己的风格"}}
