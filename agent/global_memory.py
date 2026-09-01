"""用户本人的全局记忆，不与某一联系人绑定。"""

import json
import os
from datetime import datetime

from agent.paths import global_memory_dir, global_memory_path, global_memory_transcript_path


GLOBAL_MEMORY_PROMPT = """你是 LearnLove 的用户全局记忆整理员。根据已有全局记忆和新增原始记录，整理服务于用户本人的长期背景，而不是某个联系人的档案。

固定使用以下 Markdown 小节：
## 1. 用户偏好与边界
## 2. 长期目标与进行中的项目
## 3. 稳定事实与重要决定
## 4. 当前待办与开放问题
## 5. 用户明确要求记住

保留用户明确表达的偏好、目标、限制、路径、决定、承诺和未完成事项。冲突信息同时保留并标注待确认；不要把助手的猜测写成事实。删除寒暄和机械重复，输出不超过 {max_chars} 字符。直接输出 Markdown 正文。"""


class GlobalMemory:
    """维护可跨联系人、跨咨询模式使用的用户全局记忆。"""

    def __init__(self, llm_config: dict | None = None):
        self.memory_path = global_memory_path()
        self.transcript_path = global_memory_transcript_path()
        self.llm_config = llm_config or {}
        self._memory_text = ""
        self._lines: list[str] = []

    def startup(self) -> str:
        """读取已经整理的全局记忆和待整理记录。"""
        os.makedirs(global_memory_dir(), exist_ok=True)
        if os.path.exists(self.memory_path):
            self._memory_text = open(self.memory_path, encoding="utf-8").read()
        if os.path.exists(self.transcript_path):
            self._lines = [line for line in open(self.transcript_path, encoding="utf-8") if line.strip()]
        return self._memory_text

    def memory_text(self) -> str:
        """返回当前可注入模型的全局记忆正文。"""
        return self._memory_text

    def remember(self, content: str) -> str:
        """立即写入用户明确要求记住的内容，并同步到可注入记忆。"""
        text = str(content or "").strip()
        if not text:
            raise ValueError("全局记忆内容不能为空")
        if "## 5. 用户明确要求记住" not in self._memory_text:
            prefix = self._memory_text.rstrip()
            self._memory_text = (prefix + "\n\n" if prefix else "") + "## 5. 用户明确要求记住\n"
        entry = f"- [{datetime.now().strftime('%Y-%m-%d')}] {text}"
        self._memory_text = self._memory_text.rstrip() + "\n" + entry + "\n"
        with open(self.memory_path, "w", encoding="utf-8") as file:
            file.write(self._memory_text)
        return entry

    def log_turn(self, user_content: str, assistant_content: str = "") -> None:
        """追加用户与助手对话，供达到阈值后统一整理。"""
        entries = []
        if user_content:
            entries.append({"role": "user", "content": user_content})
        if assistant_content:
            entries.append({"role": "assistant", "content": assistant_content})
        if not entries:
            return
        os.makedirs(global_memory_dir(), exist_ok=True)
        with open(self.transcript_path, "a", encoding="utf-8") as file:
            for entry in entries:
                line = json.dumps({"at": datetime.now().isoformat(), **entry}, ensure_ascii=False)
                file.write(line + "\n")
                self._lines.append(line)

    def needs_compact(self, max_chars: int = 30000, max_lines: int = 30) -> bool:
        """判断待整理记录是否需要压缩进长期全局记忆。"""
        return len(self._lines) >= max_lines or sum(map(len, self._lines)) >= max_chars

    def compact(self, max_memory_chars: int = 5000) -> bool:
        """用 LLM 合并全局记忆；失败时绝不清空原始记录。"""
        if not self._lines:
            return False
        from agent.llm import chat_simple

        raw = "\n".join(self._lines)
        prompt = f"## 已有全局记忆\n{self._memory_text or '(无)'}\n\n## 新增原始记录\n{raw}"
        result = chat_simple(prompt, GLOBAL_MEMORY_PROMPT.format(max_chars=max_memory_chars), self.llm_config)
        if not result or result.startswith("[LLM 错误]"):
            return False
        with open(self.memory_path, "w", encoding="utf-8") as file:
            file.write(result)
        with open(self.transcript_path, "w", encoding="utf-8") as file:
            file.write("")
        self._memory_text = result
        self._lines = []
        return True
