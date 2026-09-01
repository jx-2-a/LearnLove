"""
工具注册表 — 将工具名称映射到处理函数

所有处理函数签名: func(**kwargs) -> dict
返回格式: {"ok": bool, "data": ..., "error": str}
"""

import os
import json
from datetime import datetime

# 直接导入 — 工具模块
from agent.tools.contact import find_contact, list_contacts, switch_contact
from agent.tools.message import get_chat_history, check_new_messages, search_messages, get_live_feed
from agent.tools.decode import decode_voice, batch_decode_voices, set_voice_mode
from agent.tools.event import record_event, view_events
from agent.tools.conversation import (
    import_hub_conversation,
    list_saved_conversations,
    read_conversation_history,
    search_conversation_history,
)
from agent.media_api import media_api_status, process_media_queue
from agent.tools.send import copy_to_clipboard, auto_send, check_wechat_window
from agent.tools.monitor import _start_monitoring_raw, stop_monitoring, check_monitor_status
from agent.tools.review import list_reviews, read_review
from agent.protocol import ok


# ===== 内部处理函数（避免循环依赖）=====

def _view_memory_handler(contact_name: str = "") -> dict:
    from agent.tools._state import state
    from agent.paths import memory_md_path
    name = contact_name or state.active_contact_name
    if not name:
        return {"ok": False, "error": "未指定联系人"}
    mem_path = memory_md_path(name)
    if os.path.exists(mem_path):
        with open(mem_path, "r", encoding="utf-8") as f:
            return {"ok": True, "data": {"contact": name, "memory": f.read()}}
    return {"ok": True, "data": {"contact": name, "memory": "(尚无记忆)"}}


def _record_lesson_handler(title: str, content: str, tags: str = "",
                           contact_name: str = "") -> dict:
    from agent.tools._state import state
    from agent.paths import lessons_path, memory_dir
    name = contact_name or state.active_contact_name
    if not name:
        return {"ok": False, "error": "未指定联系人（请提供 contact_name）"}
    lp = lessons_path(name)
    os.makedirs(memory_dir(name), exist_ok=True)
    lessons = []
    if os.path.exists(lp):
        with open(lp, "r", encoding="utf-8") as f:
            lessons = json.load(f)
    lessons.append({
        "id": str(len(lessons) + 1),
        "title": title,
        "content": content,
        "tags": tags,
        "date": datetime.now().strftime("%Y-%m-%d"),
    })
    with open(lp, "w", encoding="utf-8") as f:
        json.dump(lessons, f, ensure_ascii=False, indent=2)
    return {"ok": True, "data": {"recorded": title, "contact": name}}


def _list_lessons_handler(contact_name: str = "") -> dict:
    from agent.tools._state import state
    from agent.paths import lessons_path
    name = contact_name or state.active_contact_name
    if not name:
        return {"ok": True, "data": {"lessons": [], "hint": "未指定联系人"}}
    lp = lessons_path(name)
    if os.path.exists(lp):
        with open(lp, "r", encoding="utf-8") as f:
            return {"ok": True, "data": {"contact": name, "lessons": json.load(f)}}
    return {"ok": True, "data": {"contact": name, "lessons": []}}


def _record_note_handler(title: str = "", content: str = "", contact_name: str = "") -> dict:
    """留档一条「当时」的重要内容（时间点快照，永不压缩、不自动注入）。

    无活动联系人时落到固定桶「自己」，保证纯咨询场景也能留档。
    """
    from agent.tools._state import state
    from agent.paths import notes_path, memory_dir
    name = contact_name or state.active_contact_name or "自己"
    if not content or not content.strip():
        return {"ok": False, "error": "内容不能为空"}
    np = notes_path(name)
    os.makedirs(memory_dir(name), exist_ok=True)
    notes = []
    if os.path.exists(np):
        try:
            with open(np, "r", encoding="utf-8") as f:
                notes = json.load(f)
        except (json.JSONDecodeError, OSError):
            notes = []
    notes.append({
        "id": str(len(notes) + 1),
        "title": title,
        "content": content,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    with open(np, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
    return {"ok": True, "data": {"recorded": title or content[:30], "contact": name, "count": len(notes)}}


def _view_notes_handler(contact_name: str = "", limit: int = 10,
                        max_chars: int = 4000, keyword: str = "") -> dict:
    """按需读取内容留档：倒序（最新在前）、带日期、可按 keyword 搜索、可截断。"""
    from agent.tools._state import state
    from agent.paths import notes_path
    name = contact_name or state.active_contact_name or "自己"
    np = notes_path(name)
    notes = []
    if os.path.exists(np):
        try:
            with open(np, "r", encoding="utf-8") as f:
                notes = json.load(f)
        except (json.JSONDecodeError, OSError):
            notes = []
    if not notes:
        return {"ok": True, "data": {"contact": name, "notes": [], "count": 0, "total": 0}}
    if keyword:
        kw = keyword.strip().lower()
        notes = [n for n in notes if kw in (n.get("title", "") + n.get("content", "")).lower()]
    total = len(notes)
    selected = notes[-limit:][::-1]  # 最新在前
    result = []
    budget = 0
    for n in selected:
        entry = {
            "id": n.get("id", ""),
            "title": n.get("title", ""),
            "content": n.get("content", ""),
            "date": n.get("date", ""),
        }
        remain = max_chars - budget
        if remain <= 0:
            break
        if len(entry["content"]) > remain:
            entry["content"] = entry["content"][:remain] + "..."
            result.append(entry)
            break
        result.append(entry)
        budget += len(entry["content"])
    return {"ok": True, "data": {"contact": name, "notes": result, "count": len(result), "total": total}}


def _express_translate_handler(meaning: str, tone: str = "warm",
                               context_note: str = "") -> dict:
    from agent.tools.expression import express_translate
    return express_translate(meaning, tone=tone, context_note=context_note)


def _view_output_handler(id: str, start: int = None, end: int = None,
                         grep: str = None) -> dict:
    from agent.outputs import view
    return view(id, start=start, end=end, grep=grep)


def _list_skills_handler() -> dict:
    from agent.tool_manager import skillmgr
    return skillmgr.list_skills()


def _activate_skill_handler(name: str) -> dict:
    from agent.tool_manager import skillmgr
    return skillmgr.activate_skill(name)


def _deactivate_skill_handler(name: str) -> dict:
    from agent.tool_manager import skillmgr
    return skillmgr.deactivate_skill(name)


def _start_monitoring_handler(contacts: list[str] = None, interval: float = 2.0) -> dict:
    """启动后台监听的包装器，返回可序列化的结果"""
    from agent.tools._state import state
    if not contacts:
        # 从配置获取
        cfg_contacts = state.contacts_config()
        contacts = [c["wxid"] for c in cfg_contacts if c.get("auto_monitor", True)]
    if not contacts:
        return {"ok": False, "error": "没有指定要监听的联系人"}
    t = _start_monitoring_raw(contacts, interval)
    state.monitor_thread = t
    state.monitor_running = True
    return {"ok": True, "data": {
        "contacts": contacts,
        "interval": interval,
        "thread_name": t.name,
    }}


def _analyze_my_style_handler() -> dict:
    from agent.style_profiler import analyze_my_style
    from agent.tools._state import state
    return analyze_my_style(state.config.get("llm", {}))


def _analyze_contact_style_handler(contact_name: str) -> dict:
    from agent.style_profiler import analyze_contact_style
    from agent.tools._state import state
    return analyze_contact_style(contact_name, state.config.get("llm", {}))


def _view_style_handler(contact_name: str = "") -> dict:
    from agent.style_profiler import view_style
    return view_style(contact_name)


def _peek_contact_handler(contact_name: str, sections: list[str] = None) -> dict:
    """跨联系人查看 — 不切换联系人，只读另一个联系人的记忆/风格/教训/近期对话。"""
    from agent.paths import memory_md_path, contact_style_path, lessons_path, transcript_path

    sections = sections or ["memory", "style", "lessons", "recent_transcript"]
    result = {"contact": contact_name}

    if "memory" in sections:
        p = memory_md_path(contact_name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                result["memory"] = f.read()[:2000]
        else:
            result["memory"] = "(无记忆)"

    if "style" in sections:
        p = contact_style_path(contact_name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                result["style"] = f.read()[:800]
        else:
            result["style"] = "(无风格分析)"

    if "lessons" in sections:
        p = lessons_path(contact_name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                lessons = json.load(f)
            result["lessons"] = lessons[-10:]
        else:
            result["lessons"] = []

    if "recent_transcript" in sections:
        p = transcript_path(contact_name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()[-20:]
            result["recent_transcript"] = [line.strip() for line in lines]
        else:
            result["recent_transcript"] = []

    return {"ok": True, "data": result}


# ===== 文件读取 =====

# 纯文本文件扩展名
_TEXT_EXTENSIONS = {
    '.txt', '.md', '.json', '.csv', '.yaml', '.yml', '.toml', '.ini', '.cfg',
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.xml', '.svg',
    '.log', '.sql', '.sh', '.bat', '.ps1', '.c', '.cpp', '.h', '.java', '.go',
    '.rs', '.rb', '.php', '.lua', '.r', '.jl', '.swift', '.kt',
}

# 文档格式（需要解析提取文本）
_DOC_EXTENSIONS = {'.docx'}

# 所有可读取的扩展名
_ALL_EXTENSIONS = _TEXT_EXTENSIONS | _DOC_EXTENSIONS

_MAX_FILE_SIZE = 5 * 1024 * 1024  # 文档类上限 5MB（DOCX 比纯文本大）


def _parse_docx(filepath: str) -> str:
    """从 .docx 文件中提取纯文本（零依赖，仅用 stdlib zip + xml）"""
    import zipfile
    import xml.etree.ElementTree as ET

    # DOCX 的文本命名空间
    NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

    with zipfile.ZipFile(filepath, 'r') as zf:
        # 检查必要文件
        if 'word/document.xml' not in zf.namelist():
            raise ValueError("无效的 docx 文件：缺少 word/document.xml")

        paragraphs = []

        # 读取正文
        doc_xml = zf.read('word/document.xml')
        root = ET.fromstring(doc_xml)
        for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
            texts = []
            for t in p.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
                if t.text:
                    texts.append(t.text)
            line = ''.join(texts)
            paragraphs.append(line)

        # 读取脚注（如果有）
        if 'word/footnotes.xml' in zf.namelist():
            fn_xml = zf.read('word/footnotes.xml')
            fn_root = ET.fromstring(fn_xml)
            fn_paras = []
            for p in fn_root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                texts = []
                for t in p.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
                    if t.text:
                        texts.append(t.text)
                line = ''.join(texts)
                if line:
                    fn_paras.append(f'[脚注] {line}')
            if fn_paras:
                paragraphs.append('')
                paragraphs.extend(fn_paras)

    return '\n'.join(paragraphs)


def _get_current_time_handler() -> dict:
    """返回当前日期时间信息，让 agent 知道「现在是什么时候」"""
    from datetime import datetime
    now = datetime.now()
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return {
        "ok": True,
        "data": {
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "year": now.year,
            "month": now.month,
            "day": now.day,
            "hour": now.hour,
            "minute": now.minute,
            "weekday": weekday_cn[now.weekday()],
            "weekday_en": now.strftime("%A"),
            "unix_ts": now.timestamp(),
            "iso": now.isoformat(),
        },
    }


def _read_file_handler(path: str) -> dict:
    """读取本地文件（纯文本 + docx 文档）"""
    if not path or not isinstance(path, str):
        return {"ok": False, "error": "请提供有效的文件路径"}

    # 展开 ~ 为家目录
    path = os.path.expanduser(path)

    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"路径不是文件: {path}"}

    # 检查扩展名
    ext = os.path.splitext(path)[1].lower()
    if ext not in _ALL_EXTENSIONS:
        return {"ok": False, "error": f"不支持的文件类型: {ext}。支持的格式: {', '.join(sorted(_ALL_EXTENSIONS))}"}

    # 检查文件大小
    size = os.path.getsize(path)
    if size > _MAX_FILE_SIZE:
        return {"ok": False, "error": f"文件太大 ({size / 1024:.0f} KB)，最大允许 {_MAX_FILE_SIZE / 1024:.0f} KB"}

    # 根据类型读取
    if ext in _DOC_EXTENSIONS:
        # DOCX 文档
        try:
            content = _parse_docx(path)
        except Exception as e:
            return {"ok": False, "error": f"解析文档失败: {e}"}
    else:
        # 纯文本
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            try:
                with open(path, "r", encoding="gbk") as f:
                    content = f.read()
            except UnicodeDecodeError:
                return {"ok": False, "error": f"无法解码文件内容（尝试了 UTF-8 和 GBK）: {path}"}
        except PermissionError:
            return {"ok": False, "error": f"没有权限读取文件: {path}"}
        except Exception as e:
            return {"ok": False, "error": f"读取文件失败: {e}"}

    filename = os.path.basename(path)
    lines = content.count("\n") + 1
    chars = len(content)

    return {
        "ok": True,
        "data": {
            "path": path,
            "filename": filename,
            "size": size,
            "lines": lines,
            "chars": chars,
            "content": content,
        },
    }


# ===== 工具映射 =====

TOOL_MAP = {
    # 联系人
    "find_contact": find_contact,
    "list_contacts": list_contacts,
    "switch_contact": switch_contact,
    # 消息
    "get_chat_history": get_chat_history,
    "check_new_messages": check_new_messages,
    "search_messages": search_messages,
    "get_live_feed": get_live_feed,
    # 语音
    "decode_voice": decode_voice,
    "set_voice_mode": set_voice_mode,
    "media_api_status": media_api_status,
    "process_media_queue": process_media_queue,
    # 发送
    "copy_reply": copy_to_clipboard,
    "copy_to_clipboard": copy_to_clipboard,
    "auto_send_message": auto_send,
    "auto_send": auto_send,
    "check_wechat_window": check_wechat_window,
    # 监控
    "start_monitoring": _start_monitoring_handler,
    "stop_monitoring": stop_monitoring,
    "check_monitor_status": check_monitor_status,
    # 记忆
    "view_memory": _view_memory_handler,
    "record_lesson": _record_lesson_handler,
    "list_lessons": _list_lessons_handler,
    # 留档（时间点快照，按需读取）
    "record_note": _record_note_handler,
    "view_notes": _view_notes_handler,
    # LearnLove 独立对话账本与 Hub 归档导入
    "search_conversation_history": search_conversation_history,
    "read_conversation_history": read_conversation_history,
    "list_saved_conversations": list_saved_conversations,
    "import_hub_conversation": import_hub_conversation,
    # 事件/故事（自动注入近期摘要，保留修订历史和证据消息 ID）
    "record_event": record_event,
    "view_events": view_events,
    # 表达翻译
    "express_translate": _express_translate_handler,
    # 技能管理
    "list_skills": _list_skills_handler,
    "activate_skill": _activate_skill_handler,
    "deactivate_skill": _deactivate_skill_handler,
    # 风格分析
    "analyze_my_style": _analyze_my_style_handler,
    "analyze_contact_style": _analyze_contact_style_handler,
    "view_style": _view_style_handler,
    # 跨联系人
    "peek_contact": _peek_contact_handler,
    # 文件
    "read_file": _read_file_handler,
    # 时间
    "get_current_time": _get_current_time_handler,
    # 复盘报告
    "list_reviews": list_reviews,
    "read_review": read_review,
    # 系统
    "view_output": _view_output_handler,
}


# ===== 分发 =====

def dispatch(tool_name: str, **kwargs) -> dict:
    """分发工具调用。未找到时返回 error。"""
    handler = TOOL_MAP.get(tool_name)
    if handler is None:
        # 尝试 SkillManager 的技能工具
        try:
            from agent.tool_manager import skillmgr
            result = skillmgr.dispatch_skill_tool(tool_name, **kwargs)
            if result is not None:
                return result
        except Exception:
            pass
        return {"ok": False, "error": f"未知工具: {tool_name}"}
    try:
        return handler(**kwargs)
    except Exception as e:
        import traceback
        return {"ok": False, "error": f"工具执行异常: {e}\n{traceback.format_exc()}"}
