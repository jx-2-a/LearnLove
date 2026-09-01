import json

import agent.archive
from agent.conversation_store import (
    import_hub_archive,
    list_conversation_sessions,
    read_conversation_entry,
    restore_conversation_messages,
    save_conversation_entry,
    search_conversations,
)


def _temporary_archive(monkeypatch, tmp_path):
    """把统一归档数据库和媒体目录隔离到测试目录。"""
    monkeypatch.setattr(agent.archive, "records_db_path", lambda: str(tmp_path / "records.db"))
    monkeypatch.setattr(agent.archive, "archived_media_dir", lambda: str(tmp_path / "media"))


def test_conversation_ledger_saves_searches_and_reads_full_text(monkeypatch, tmp_path):
    """搜索可返回摘要，但 entry_id 必须能取回未裁剪原文。"""
    _temporary_archive(monkeypatch, tmp_path)
    full = "关键承诺" + "很长的原文" * 1000 + "END-MARKER"
    saved = save_conversation_entry(
        role="user", content=full, session_key="hub:12", contact_name="A",
    )

    results = search_conversations(keyword="关键承诺", contact_name="A")
    assert saved["inserted"]
    assert len(results) == 1
    assert results[0]["truncated"] is True
    restored = read_conversation_entry(results[0]["entry_id"])
    assert restored["content"] == full


def test_hub_archive_import_reconstructs_messages_and_is_idempotent(monkeypatch, tmp_path):
    """优先采用 assistant_final；只有无 final 时才拼接流式增量。"""
    _temporary_archive(monkeypatch, tmp_path)
    archive = tmp_path / "8.jsonl"
    events = [
        {"type": "meta", "label": "恋爱军师"},
        {"type": "user", "text": "第一问", "turn": 1},
        {"type": "assistant_delta", "content": "临时"},
        {"type": "assistant_delta", "content": "流式"},
        {"type": "assistant_final", "content": "第一答完整版"},
        {"type": "tool_start", "id": "t1", "name": "search", "args": {"q": "x"}},
        {"type": "tool_end", "id": "t1", "name": "search", "ok": True},
        {"type": "user", "text": "第二问", "turn": 2},
        {"type": "assistant_delta", "content": "第二答"},
        {"type": "assistant_delta", "content": "流式完成"},
        {"type": "assistant_end"},
        {"type": "log", "text": "系统提示", "level": "info"},
    ]
    archive.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )

    first = import_hub_archive("8", str(archive), contact_name="A")
    second = import_hub_archive("8", str(archive), contact_name="A")
    messages = restore_conversation_messages(session_key="hub-archive:8", limit=100)

    assert first["entries_inserted"] == 7
    assert second["entries_inserted"] == 0
    assert messages == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答完整版"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答流式完成"},
    ]
    assert "临时流式" not in "\n".join(m["content"] for m in messages)
    sessions = list_conversation_sessions()
    assert any(item["source_sid"] == "8" for item in sessions)


def test_restore_can_target_live_session_key(monkeypatch, tmp_path):
    """非 Hub 导入记录也能按 LearnLove 会话键恢复。"""
    _temporary_archive(monkeypatch, tmp_path)
    save_conversation_entry("user", "问题", session_key="hub:99")
    save_conversation_entry("assistant", "回答", session_key="hub:99")

    assert restore_conversation_messages(session_key="hub:99") == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
