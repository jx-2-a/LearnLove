"""
每联系人记忆系统 — transcript.jsonl + LLM 压缩 → memory.md

每个联系人在 data/agent/memory/<name>/ 下拥有：
  transcript.jsonl  — 追加式对话记录
  memory.md         — LLM 压缩后的结构记忆（5段）
  archive/          — 旧 transcript 归档

memory.md 五段结构：
  1. 关系阶段与目标
  2. 对方关键信息（喜好、习惯、重要日期、性格特征）
  3. 近期对话摘要
  4. 沟通经验教训（什么有效/无效）
  5. 待办事项（约定、承诺、需要关心的事）
"""

import os
import json
from datetime import datetime

from agent.paths import memory_dir, memory_md_path, transcript_path, archive_dir

COMPRESS_SYSTEM_PROMPT = """你是恋爱聊天助手的记忆整理员。把「旧的长期记忆」和「新的对话记录」合并压缩成一份新的长期记忆。

要求：
- 用 Markdown，固定五个小节：
  ## 1. 关系阶段与目标
  ## 2. 对方关键信息（喜好、习惯、重要日期、性格特征）
  ## 3. 近期对话摘要
  ## 4. 沟通经验教训（哪些回复方式有效/无效）
  ## 5. 待办事项（约定的事情、需要关心的话题）
- 必须保留姓名、日期、承诺、边界、偏好、冲突原因、未完成事项及用户明确要求记住的内容
- 新记录与旧记忆冲突时保留双方说法并标注待确认，不可擅自覆盖
- 可以丢弃无信息量的寒暄和完全重复内容，但不可因为内容位于中间而省略
- 总长度不超过 {max_chars} 字符
- 直接输出 Markdown 正文，不要前言和结尾"""


SESSION_COMPACT_PROMPT = """你是会话上下文整理器。把「已有早期会话摘要」与「下一批原始会话」合并成可供后续继续对话的保真摘要。

必须保留：
- 用户的目标、要求、偏好、否定意见和明确要求记住的信息
- 已确认事实、姓名、日期、数字、路径、错误信息和关键原话
- 已做决定、采取过的操作、结果、未完成事项和下一步
- 不确定、冲突或失败之处，不能把推测写成事实

按主题组织 Markdown。删除纯寒暄与机械重复，但不要只保留首尾。直接输出摘要正文。"""


def _lossless_chunks(lines: list[str], max_chars: int) -> list[str]:
    """按完整记录分批；超长单条拆成连续片段，确保每个字符都进入某一批。"""
    limit = max(1000, int(max_chars or 0))
    chunks = []
    current = []
    current_size = 0

    def flush_current():
        """提交当前批次。"""
        nonlocal current, current_size
        if current:
            chunks.append("\n".join(current))
            current = []
            current_size = 0

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for index, piece in enumerate(pieces):
            if len(pieces) > 1:
                piece = f"[同一记录片段 {index + 1}/{len(pieces)}]\n{piece}"
            added = len(piece) + (1 if current else 0)
            if current and current_size + added > limit:
                flush_current()
            current.append(piece)
            current_size += len(piece) + (1 if current_size else 0)
    flush_current()
    return chunks


def compact_session_history(messages: list[dict], llm_config: dict,
                            input_budget: int, keep_recent_messages: int = 40):
    """接近窗口时分批摘要早期会话；失败则原样返回，绝不静默裁剪。"""
    from agent.context import estimate_messages_tokens, estimate_text_tokens
    from agent.llm import chat_simple

    original = list(messages or [])
    if estimate_messages_tokens(original) <= int(input_budget * 0.82):
        return original, False

    protected = max(2, int(keep_recent_messages or 0))
    if len(original) <= protected:
        return original, False

    older = original[:-protected]
    recent = original[-protected:]
    # 中文接近 1 字符/token，批次按 token 预算的 40% 控制，给旧摘要和提示词留余量。
    batch_chars = max(1000, int(input_budget * 0.40))
    serialized = [json.dumps(m, ensure_ascii=False, default=str) for m in older]
    batches = _lossless_chunks(serialized, batch_chars)
    summary = ""

    try:
        for batch in batches:
            user_msg = (
                f"## 已有早期会话摘要\n{summary or '(无)'}\n\n"
                f"## 下一批原始会话\n{batch}\n\n请输出合并后的保真摘要。"
            )
            # 摘要本身若异常膨胀，停止替换；保留原始历史更安全。
            if estimate_text_tokens(user_msg) >= input_budget:
                return original, False
            result = chat_simple(user_msg, SESSION_COMPACT_PROMPT, llm_config)
            if not result or result.startswith("[LLM 错误]"):
                return original, False
            summary = result
    except Exception:
        return original, False

    compacted = [{
        "role": "system",
        "content": "## 早期会话保真摘要（原文已写入 transcript/归档）\n" + summary,
    }] + recent
    if estimate_messages_tokens(compacted) >= estimate_messages_tokens(original):
        return original, False
    return compacted, True


class ContactMemory:
    """单个联系人的记忆管理器"""

    def __init__(self, contact_name: str, llm_config: dict = None):
        self.contact_name = contact_name
        self.contact_dir = memory_dir(contact_name)
        self.transcript_path = transcript_path(contact_name)
        self.memory_path = memory_md_path(contact_name)
        self.archive_dir = archive_dir(contact_name)
        self.llm_config = llm_config or {}
        self._turn_count = 0
        self._memory_text = ""
        self._transcript_lines = []

    def startup(self) -> str:
        """启动时加载 memory.md。返回记忆文本用于注入系统提示词。"""
        os.makedirs(self.contact_dir, exist_ok=True)
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                self._memory_text = f.read()

        # 加载已有 transcript 计数
        if os.path.exists(self.transcript_path):
            with open(self.transcript_path, "r", encoding="utf-8") as f:
                self._transcript_lines = [line for line in f if line.strip()]

        self._turn_count = len(self._transcript_lines)
        return self._memory_text

    def memory_text(self) -> str:
        """当前记忆内容"""
        return self._memory_text

    def log_turn(self, incoming_msg: str, incoming_sender: str,
                 suggested_reply: str = "", actual_reply: str = "",
                 ts: str = None):
        """记录一轮对话"""
        if ts is None:
            ts = datetime.now().strftime("%m-%d %H:%M")

        entries = []
        if incoming_msg:
            entries.append({
                "ts": ts,
                "role": "contact",
                "sender": incoming_sender,
                "content": incoming_msg,
            })
        if suggested_reply:
            entries.append({
                "ts": ts,
                "role": "suggestion",
                "content": suggested_reply,
            })
        if actual_reply:
            entries.append({
                "ts": ts,
                "role": "user",
                "content": actual_reply,
            })

        os.makedirs(self.contact_dir, exist_ok=True)
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._transcript_lines.append(json.dumps(entry, ensure_ascii=False))

        self._turn_count += len(entries)

    def flush(self, messages: list[dict]):
        """将近期 LLM 对话消息追加到 transcript（类似参考 memory.py 的 flush()）。

        用于记录用户的提问和 AI 的建议，与 log_turn 互补。
        """
        os.makedirs(self.contact_dir, exist_ok=True)
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            written = 0
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "assistant"):
                    entry = {
                        "ts": datetime.now().strftime("%m-%d %H:%M"),
                        "role": role,
                        "content": content,
                    }
                    line = json.dumps(entry, ensure_ascii=False)
                    f.write(line + "\n")
                    self._transcript_lines.append(line)
                    written += 1

        self._turn_count += written

    def needs_compact(self, max_chars: int = 60000, max_turns: int = 30) -> bool:
        """是否需要压缩"""
        # 按轮数
        if max_turns > 0 and self._turn_count >= max_turns:
            return True
        # 按字符数
        transcript_size = sum(len(l) for l in self._transcript_lines)
        if transcript_size >= max_chars:
            return True
        return False

    def compact(self, max_memory_chars: int = 4000,
                transcript_max_chars: int = 60000) -> bool:
        """分批合并全部 transcript；任一批失败时保留原记录与旧记忆。"""
        if not self._transcript_lines:
            return False

        from agent.llm import chat_simple

        # 按完整记录分批，所有待处理字符都会进入某一批，不再只保留头尾。
        batches = _lossless_chunks(self._transcript_lines, transcript_max_chars)
        prompt = COMPRESS_SYSTEM_PROMPT.replace("{max_chars}", str(max_memory_chars))
        merged_memory = self._memory_text
        try:
            for batch_index, transcript_text in enumerate(batches, start=1):
                user_msg = (
                    f"## 旧记忆\n{merged_memory or '(无)'}\n\n"
                    f"## 新对话（第 {batch_index}/{len(batches)} 批）\n{transcript_text}\n\n"
                    "请合并输出新的 memory.md"
                )
                result = chat_simple(
                    user_message=user_msg,
                    system_prompt=prompt,
                    config=self.llm_config,
                )
                if not result or result.startswith("[LLM 错误]"):
                    return False
                merged_memory = result
        except Exception:
            return False

        # 必须先完整归档；归档失败时绝不清空 transcript。
        os.makedirs(self.archive_dir, exist_ok=True)
        archive_name = f"transcript-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jsonl"
        archive_path = os.path.join(self.archive_dir, archive_name)
        try:
            with open(archive_path, "w", encoding="utf-8") as f:
                for line in self._transcript_lines:
                    f.write(line.rstrip("\r\n") + "\n")
        except Exception:
            return False

        # 原子替换 memory.md，避免进程中断留下半个文件。
        header = f"<!-- updated: {datetime.now().isoformat()} turns: {self._turn_count} -->\n\n"
        temp_memory_path = self.memory_path + ".tmp"
        try:
            with open(temp_memory_path, "w", encoding="utf-8") as f:
                f.write(header + merged_memory)
            os.replace(temp_memory_path, self.memory_path)
        except Exception:
            try:
                if os.path.exists(temp_memory_path):
                    os.remove(temp_memory_path)
            except Exception:
                pass
            return False

        self._memory_text = merged_memory
        self._transcript_lines = []
        self._turn_count = 0

        # 重置 transcript.jsonl
        with open(self.transcript_path, "w", encoding="utf-8") as f:
            f.write("")

        return True

    def reset_baseline(self, messages: list[dict]):
        """标记当前消息长度为未 flush 的基线（类似参考 memory.py）"""
        pass  # 简化实现：log_turn 和 flush 已覆盖
