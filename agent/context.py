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
import math
from datetime import datetime

from agent.paths import lessons_path as _lessons_path


def estimate_text_tokens(text: str) -> int:
    """保守估算中英混合文本 token 数，不依赖特定模型 tokenizer。"""
    text = str(text or "")
    cjk = 0
    other = 0
    for char in text:
        code = ord(char)
        if (0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
                or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF):
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表的输入 token，包含角色和协议包装开销。"""
    total = 0
    for message in messages or []:
        total += 8
        total += estimate_text_tokens(message.get("content") or "")
        total += estimate_text_tokens(message.get("reasoning_content") or "")
        if message.get("tool_calls"):
            import json
            total += estimate_text_tokens(json.dumps(
                message["tool_calls"], ensure_ascii=False, default=str,
            ))
    return total


def resolve_context_window(llm_config: dict, context_config: dict | None = None) -> int:
    """解析模型上下文窗口；默认优先采用模型档案，旧 8K 配置不再误限流。"""
    llm_config = llm_config or {}
    context_config = context_config or {}
    model_limit = int(llm_config.get("max_context_tokens") or 0)
    configured_limit = int(context_config.get("max_context_tokens") or 0)
    use_model_limit = context_config.get("use_model_context_window", True)

    if use_model_limit and model_limit > 0:
        return model_limit
    if configured_limit > 0:
        return configured_limit
    if model_limit > 0:
        return model_limit
    return 8000


def input_token_budget(llm_config: dict, context_config: dict | None = None) -> int:
    """从总窗口扣除最大输出和协议安全余量，得到可用输入预算。"""
    window = resolve_context_window(llm_config, context_config)
    output = max(1, int((llm_config or {}).get("max_tokens") or 4096))
    safety = max(2048, math.ceil(window * 0.02))
    return max(1024, window - output - safety)


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
理解对方的情绪和意图，然后给出合适的回复建议。

{contact_context}
{skill_context}
{memory_context}
{lessons_context}
{style_context}

## 回复生成原则
- **像用户本人说话**：参考用户风格分析，用类似的句式、语气、表情习惯
- 短句自然：用短句，不同方向换行分开，像真人聊天
- 批量处理：多条新消息一起看，给一条回复，不逐条分析
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
                               global_memory_text: str = "",
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

        # Layer 3: 用户全局记忆与联系人记忆
        memory_context = ""
        if global_memory_text:
            memory_context = f"## 用户全局记忆（跨联系人、跨咨询模式）\n{global_memory_text}"
        if memory_text:
            personal = f"## 长期记忆（关于 {contact_name}）\n{memory_text}"
            memory_context = f"{memory_context}\n\n{personal}" if memory_context else personal

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

        messages = [{"role": "system", "content": system_content}]
        if contact_name:
            try:
                from agent.archive import recent_events_context
                event_context = recent_events_context(contact_name)
                if event_context:
                    messages.append({"role": "system", "content": event_context})
            except Exception:
                # 归档损坏不能阻断基础聊天；工具调用时会返回明确错误。
                pass
        return messages

    def build_context(self, contact_name: str, contact_wxid: str,
                      new_message: dict | None = None,
                      new_messages: list[dict] | None = None,
                      recent_history: list[dict] = None,
                      memory_text: str = "",
                      global_memory_text: str = "",
                      skill_modifiers: str = "",
                      max_tokens: int = 8000) -> list[dict]:
        """组装完整上下文

        Args:
            contact_name: 联系人显示名
            contact_wxid: 联系人 wxid
            new_message: (deprecated) 单条新消息，建议用 new_messages
            new_messages: 批量新消息列表，每条 {"sender": str, "content": str, "time": str}
            recent_history: 最近的对话历史（作为参考背景）
            memory_text: 长期记忆内容
            global_memory_text: 用户本人跨联系人的长期记忆内容
            skill_modifiers: 活跃技能的 prompt_modifier 拼接
            max_tokens: token 预算上限

        Returns:
            messages 列表，可直接传入 llm.chat()
        """
        messages = self.build_system_messages(
            contact_name=contact_name,
            contact_wxid=contact_wxid,
            memory_text=memory_text,
            global_memory_text=global_memory_text,
            skill_modifiers=skill_modifiers,
        )

        # Layer 5: 近期聊天历史（参考背景）
        if recent_history:
            history_text = self._format_history(recent_history, max_tokens)
            if history_text:
                messages.append({
                    "role": "system",
                    "content": f"## 之前的对话（参考背景）\n{history_text}",
                })

        # Layer 6: 新消息 — 支持批量
        # 兼容旧的 new_message 参数
        all_new = new_messages or []
        if new_message and not all_new:
            all_new = [new_message]

        if all_new:
            # 拆分为对方的消息（需要回复）和自己的消息（上下文参考）
            from_contact = [m for m in all_new if not m.get("is_self")]
            from_self = [m for m in all_new if m.get("is_self")]

            parts = []

            if from_self:
                # 用户在此期间发的消息——给 LLM 作为上下文
                self_lines = ["[你在此期间发的消息 — 参考上下文]"]
                for m in from_self:
                    self_lines.append(f"[{m.get('time', '')}] 你: {m['content']}")
                parts.append("\n".join(self_lines))

            if from_contact:
                # 对方发的新消息——需要回复
                if len(from_contact) == 1:
                    m = from_contact[0]
                    parts.append(
                        f"[新消息 — 需要回复]\n"
                        f"{m['sender']} ({m.get('time', '')}): {m['content']}"
                    )
                else:
                    contact_lines = [f"[新消息 ×{len(from_contact)} — 以下是上次回复后对方发来的消息，一起看，给一条回复]"]
                    for m in from_contact:
                        contact_lines.append(f"[{m.get('time', '')}] {m['sender']}: {m['content']}")
                    parts.append("\n".join(contact_lines))

            if parts:
                user_content = "\n\n".join(parts)
                messages.append({"role": "user", "content": user_content})

        return messages

    def _format_history(self, history: list[dict], max_tokens: int) -> str:
        """完整格式化对话历史；预算控制由上层保真压缩负责。"""
        lines = []
        for entry in history:
            sender = entry.get("sender", "?")
            content = entry.get("content", "")
            line = f"[{entry.get('time', '')}] {sender}: {content}"
            lines.append(line)
        return "\n".join(lines)

    def estimate_tokens(self, messages: list[dict]) -> int:
        """保守估算中英混合消息 token 数。"""
        return estimate_messages_tokens(messages)
