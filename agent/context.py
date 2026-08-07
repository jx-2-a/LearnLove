"""
上下文组装器 — 每次 LLM 调用前组装完整的消息上下文

6层组装：
  Layer 0: 系统提示词模板
  Layer 1: 联系人信息
  Layer 2: 活跃技能
  Layer 3: 长期记忆
  Layer 4: 经验教训（per-contact）
  Layer 5: 近期聊天历史 + 新消息
"""

import os
from datetime import datetime

from agent.paths import lessons_path as _lessons_path


def _load_system_prompt_template() -> str:
    """加载系统提示词模板"""
    _agent_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(_agent_dir, "system_prompt.md")
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    # 默认模板
    return """你是 LearnLove Agent — 用户的微信聊天助手。

你的任务是帮助用户自然、得体地进行微信聊天。你分析对话上下文，
理解对方的情绪和意图，然后给出 1-3 条可选的回复建议。

{contact_context}
{skill_context}
{memory_context}
{lessons_context}
{style_context}

## 回复生成原则
- **像用户本人说话**：参考用户风格分析，用类似的句式、语气、表情习惯
- 短小自然：每条建议不超过 200 字，像真人聊天
- 提供选择：标注每条建议的风格差异
- 先共情再建议：理解对方情绪后再给回复方案
- 注意节奏：不要在对方情绪激动时立刻给建议"""


def _build_style_context(contact_name: str) -> str:
    """构建风格上下文：用户自己的风格 + 对方的风格"""
    parts = []
    try:
        from agent.style_profiler import load_user_style, load_contact_style

        user_style = load_user_style()
        if user_style:
            # 提取最精华的"给AI的建议"部分，加上简要总结
            parts.append("## 你的语言风格（请模仿此风格回复）\n" + user_style[:600])

        if contact_name:
            contact_style = load_contact_style(contact_name)
            if contact_style:
                parts.append(f"## {contact_name} 的语言风格（参考以更好回复对方）\n" + contact_style[:400])
    except Exception:
        pass

    return "\n\n".join(parts)


def _load_lessons(contact_name: str = "") -> str:
    """加载 per-contact 经验教训"""
    if not contact_name:
        return ""
    lp = _lessons_path(contact_name)
    if not os.path.exists(lp):
        return ""
    try:
        import json
        with open(lp, "r", encoding="utf-8") as f:
            lessons = json.load(f)
        if not lessons:
            return ""
        lines = [f"## 沟通经验教训（与 {contact_name} 的互动）"]
        for lesson in lessons[-10:]:  # 最近 10 条
            lines.append(f"- **{lesson.get('title', '')}**: {lesson.get('content', '')[:200]}")
        return "\n".join(lines)
    except Exception:
        return ""


class ContextBuilder:
    """构建每次 LLM 调用的完整上下文"""

    def __init__(self, config: dict):
        self.config = config
        self.system_template = _load_system_prompt_template()

    def build_system_messages(self, contact_name: str = "",
                               contact_wxid: str = "",
                               memory_text: str = "",
                               skill_modifiers: str = "") -> list[dict]:
        """构建系统消息层（可多条，避免单条过长）"""
        now = datetime.now()

        # Layer 1: 联系人上下文
        contact_context = ""
        if contact_name:
            contact_context = f"## 当前联系人\n- 名称: {contact_name}\n- 日期: {now.strftime('%Y年%m月%d日 %H:%M')}"
            voice_mode = "auto"
            if contact_wxid:
                from agent.tools._state import state
                voice_mode = state.voice_mode(contact_wxid)
                contact_context += f"\n- 语音模式: {voice_mode}"

        # Layer 2: 技能上下文
        skill_context = skill_modifiers if skill_modifiers else ""

        # Layer 3: 记忆上下文
        memory_context = ""
        if memory_text:
            memory_context = f"## 长期记忆（关于 {contact_name}）\n{memory_text}"

        # Layer 4: 经验教训（per-contact，每次动态加载）
        lessons_context = _load_lessons(contact_name)

        # Layer 5: 风格上下文（你的风格 + 对方的风格）
        style_context = _build_style_context(contact_name)

        # 填充模板
        system_content = self.system_template.format(
            contact_context=contact_context,
            skill_context=skill_context,
            memory_context=memory_context,
            lessons_context=lessons_context,
            style_context=style_context,
        )

        return [{"role": "system", "content": system_content}]

    def build_context(self, contact_name: str, contact_wxid: str,
                      new_message: dict | None = None,
                      recent_history: list[dict] = None,
                      memory_text: str = "",
                      skill_modifiers: str = "",
                      max_tokens: int = 8000) -> list[dict]:
        """组装完整上下文

        Args:
            contact_name: 联系人显示名
            contact_wxid: 联系人 wxid
            new_message: 新来的消息 {"sender": str, "content": str, "time": str}
            recent_history: 最近的对话历史
            memory_text: 长期记忆内容
            skill_modifiers: 活跃技能的 prompt_modifier 拼接
            max_tokens: token 预算上限

        Returns:
            messages 列表，可直接传入 llm.chat()
        """
        messages = self.build_system_messages(
            contact_name=contact_name,
            contact_wxid=contact_wxid,
            memory_text=memory_text,
            skill_modifiers=skill_modifiers,
        )

        # Layer 5: 近期聊天历史
        if recent_history:
            history_text = self._format_history(recent_history, max_tokens)
            if history_text:
                messages.append({
                    "role": "system",
                    "content": f"## 近期对话记录\n{history_text}",
                })

        # Layer 6: 新消息
        if new_message:
            user_content = f"[新消息] {new_message['sender']} ({new_message.get('time', '')}): {new_message['content']}"
            messages.append({"role": "user", "content": user_content})

        return messages

    def _format_history(self, history: list[dict], max_tokens: int) -> str:
        """格式化近期对话历史，控制长度"""
        budget_chars = max_tokens * 2  # 中文约 2 chars/token
        lines = []
        total = 0

        for entry in reversed(history):
            sender = entry.get("sender", "?")
            content = entry.get("content", "")
            line = f"[{entry.get('time', '')}] {sender}: {content}"
            total += len(line)
            if total > budget_chars * 0.7:  # 用 70% 给注入的系统消息留空间
                break
            lines.append(line)

        lines.reverse()

        # 如果历史被截断，加标记
        if len(lines) < len(history):
            lines.insert(0, f"[更早的对话已省略，共 {len(history)} 条]")

        return "\n".join(lines)

    def estimate_tokens(self, messages: list[dict]) -> int:
        """快速估算 token 数（中文约 chars/2.5）"""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return int(total_chars / 2.5)
