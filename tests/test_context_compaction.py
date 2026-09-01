import json
from pathlib import Path

import agent.llm
from agent.context import ContextBuilder, input_token_budget, resolve_context_window
from agent.memory import ContactMemory, compact_session_history
from agent.model_registry import load_registry


def _memory_at(tmp_path: Path, lines: list[str]) -> ContactMemory:
    """构造路径完全位于临时目录的联系人记忆。"""
    memory = ContactMemory("测试联系人", {})
    memory.contact_dir = str(tmp_path)
    memory.transcript_path = str(tmp_path / "transcript.jsonl")
    memory.memory_path = str(tmp_path / "memory.md")
    memory.archive_dir = str(tmp_path / "archive")
    memory._transcript_lines = list(lines)
    memory._turn_count = len(lines)
    Path(memory.transcript_path).write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )
    return memory


def test_deepseek_profile_uses_one_million_context_by_default():
    """旧 agent 8K 默认值不应盖住 DeepSeek 官网模型窗口。"""
    profile = load_registry().profile("deepseek-v4-flash")

    assert profile["max_context_tokens"] == 1_000_000
    assert resolve_context_window(profile, {"max_context_tokens": 8000}) == 1_000_000
    assert input_token_budget(profile, {"max_context_tokens": 8000}) > 900_000
    assert resolve_context_window(
        profile,
        {"use_model_context_window": False, "max_context_tokens": 8000},
    ) == 8000


def test_history_formatter_does_not_drop_middle_or_early_messages():
    """历史格式化只负责展示，不能按字符预算直接切掉早期内容。"""
    builder = ContextBuilder({})
    history = [
        {"sender": "A", "time": str(i), "content": f"HISTORY-{i}"}
        for i in range(100)
    ]

    formatted = builder._format_history(history, max_tokens=10)

    for i in range(100):
        assert f"HISTORY-{i}" in formatted


def test_flush_persists_long_messages_without_truncation(tmp_path):
    """transcript 是原文账本，长消息必须逐字保留。"""
    memory = _memory_at(tmp_path, [])
    long_text = "关键事实" + "甲" * 10_000 + "END-MARKER"

    memory.flush([{"role": "user", "content": long_text}])

    stored = json.loads(Path(memory.transcript_path).read_text(encoding="utf-8"))
    assert stored["content"] == long_text


def test_memory_compaction_covers_every_batch_before_clearing(monkeypatch, tmp_path):
    """压缩必须覆盖中间记录，成功后才归档并清空待处理文件。"""
    lines = [
        json.dumps({"role": "user", "content": f"MARKER-{i}-" + "中" * 650}, ensure_ascii=False)
        for i in range(6)
    ]
    memory = _memory_at(tmp_path, lines)
    prompts = []

    def fake_chat_simple(user_message, system_prompt, config):
        """记录每一批输入并返回稳定的小摘要。"""
        prompts.append(user_message)
        return "## 1. 关系阶段与目标\n测试\n## 5. 待办事项\n保留"

    monkeypatch.setattr(agent.llm, "chat_simple", fake_chat_simple)

    assert memory.compact(max_memory_chars=4000, transcript_max_chars=1000)
    sent = "\n".join(prompts)
    for i in range(6):
        assert f"MARKER-{i}-" in sent
    assert len(prompts) > 1
    assert Path(memory.transcript_path).read_text(encoding="utf-8") == ""
    archives = list((tmp_path / "archive").glob("transcript-*.jsonl"))
    assert len(archives) == 1
    archived = archives[0].read_text(encoding="utf-8")
    for i in range(6):
        assert f"MARKER-{i}-" in archived


def test_memory_compaction_failure_keeps_original_transcript(monkeypatch, tmp_path):
    """任一压缩批失败时不能写新记忆或删除原 transcript。"""
    lines = [f"MARKER-{i}-" + "中" * 700 for i in range(4)]
    memory = _memory_at(tmp_path, lines)
    calls = 0

    def fake_chat_simple(user_message, system_prompt, config):
        """模拟第二批 API 失败。"""
        nonlocal calls
        calls += 1
        return "摘要" if calls == 1 else "[LLM 错误] timeout"

    monkeypatch.setattr(agent.llm, "chat_simple", fake_chat_simple)

    assert not memory.compact(max_memory_chars=4000, transcript_max_chars=1000)
    assert memory._transcript_lines == lines
    raw = Path(memory.transcript_path).read_text(encoding="utf-8")
    for i in range(4):
        assert f"MARKER-{i}-" in raw
    assert not Path(memory.memory_path).exists()


def test_session_compaction_preserves_recent_and_reads_all_older(monkeypatch):
    """会话摘要逐批读取全部早期消息，并逐条原样保留近期消息。"""
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"SESSION-{i}-" + "中" * 320}
        for i in range(24)
    ]
    prompts = []

    def fake_chat_simple(user_message, system_prompt, config):
        """用短摘要模拟成功压缩。"""
        prompts.append(user_message)
        return "已保留早期目标、决定和待办"

    monkeypatch.setattr(agent.llm, "chat_simple", fake_chat_simple)
    compacted, changed = compact_session_history(
        messages, {}, input_budget=7000, keep_recent_messages=6,
    )

    assert changed
    sent = "\n".join(prompts)
    for i in range(18):
        assert f"SESSION-{i}-" in sent
    assert compacted[-6:] == messages[-6:]
    assert compacted[0]["role"] == "system"


def test_session_compaction_failure_returns_original(monkeypatch):
    """摘要失败时沿用原上下文，让调用方看见错误而不是静默丢消息。"""
    messages = [{"role": "user", "content": "中" * 500} for _ in range(20)]
    monkeypatch.setattr(
        agent.llm, "chat_simple",
        lambda *args, **kwargs: "[LLM 错误] unavailable",
    )

    result, changed = compact_session_history(
        messages, {}, input_budget=7000, keep_recent_messages=4,
    )

    assert not changed
    assert result == messages
