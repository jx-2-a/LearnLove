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
- 保留重要信息，丢弃寒暄和重复内容
- 总长度不超过 {max_chars} 字符
- 直接输出 Markdown 正文，不要前言和结尾"""


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
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "assistant"):
                    # 截断长内容
                    max_chars = 4000 if role == "assistant" else 2000
                    truncated = content[:max_chars] if len(content) > max_chars else content
                    entry = {
                        "ts": datetime.now().strftime("%m-%d %H:%M"),
                        "role": role,
                        "content": truncated,
                    }
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    self._transcript_lines.append(json.dumps(entry, ensure_ascii=False))

        self._turn_count += len(messages)

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
        """LLM 压缩：transcript + 旧 memory → 新 memory.md"""
        if not self._transcript_lines:
            return False

        from agent.llm import chat_simple

        # 准备 transcript 文本（限制长度）
        transcript_text = "\n".join(
            line.strip() for line in self._transcript_lines[-500:]  # 最近 500 条
        )
        if len(transcript_text) > transcript_max_chars:
            head = transcript_text[:transcript_max_chars // 2]
            tail = transcript_text[-(transcript_max_chars // 2):]
            transcript_text = head + "\n...[中间省略]...\n" + tail

        # 构建压缩提示词
        prompt = COMPRESS_SYSTEM_PROMPT.replace("{max_chars}", str(max_memory_chars))
        user_msg = f"""## 旧记忆\n{self._memory_text or '(无)'}\n\n## 新对话\n{transcript_text}\n\n请合并输出新的 memory.md"""

        try:
            result = chat_simple(
                user_message=user_msg,
                system_prompt=prompt,
                config=self.llm_config,
            )
        except Exception:
            return False

        if result.startswith("[LLM 错误]"):
            return False

        # 归档旧 transcript
        os.makedirs(self.archive_dir, exist_ok=True)
        archive_name = f"transcript-{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        archive_path = os.path.join(self.archive_dir, archive_name)
        try:
            with open(archive_path, "w", encoding="utf-8") as f:
                f.write("".join(self._transcript_lines))
        except Exception:
            pass

        # 写入新 memory.md
        # 在 memory.md 头部添加元信息注释
        header = f"<!-- updated: {datetime.now().isoformat()} turns: {self._turn_count} -->\n\n"
        with open(self.memory_path, "w", encoding="utf-8") as f:
            f.write(header + result)

        self._memory_text = result
        self._transcript_lines = []
        self._turn_count = 0

        # 重置 transcript.jsonl
        with open(self.transcript_path, "w", encoding="utf-8") as f:
            f.write("")

        return True

    def reset_baseline(self, messages: list[dict]):
        """标记当前消息长度为未 flush 的基线（类似参考 memory.py）"""
        pass  # 简化实现：log_turn 和 flush 已覆盖
