"""对话账本工具：搜索、精确读取、列出会话和导入 Hub 归档。"""

from agent.conversation_store import (
    import_hub_archive,
    list_conversation_sessions,
    read_conversation_entry,
    search_conversations,
)
from agent.protocol import err, ok


def search_conversation_history(keyword: str = "", contact_name: str = "",
                                source_sid: str = "", role: str = "",
                                limit: int = 20, offset: int = 0) -> dict:
    """搜索 LearnLove 自动保存的全部对话。"""
    return ok({"entries": search_conversations(
        keyword=keyword, contact_name=contact_name, source_sid=source_sid,
        role=role, limit=limit, offset=offset,
    )})


def read_conversation_history(entry_id: str) -> dict:
    """读取搜索结果对应的完整单条对话。"""
    entry = read_conversation_entry(entry_id)
    return ok(entry) if entry else err(f"未找到对话记录: {entry_id}")


def list_saved_conversations(limit: int = 50) -> dict:
    """列出本地账本中的会话与 Hub 归档来源。"""
    return ok({"sessions": list_conversation_sessions(limit=limit)})


def import_hub_conversation(source_sid: str, archive_path: str = "",
                            contact_name: str = "") -> dict:
    """把 Hub 永久归档导入本地账本；重复执行会按事件行幂等去重。"""
    try:
        return ok(import_hub_archive(source_sid, archive_path, contact_name))
    except (FileNotFoundError, OSError, ValueError) as exc:
        return err(str(exc))
