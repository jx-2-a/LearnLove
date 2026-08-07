"""
表达翻译工具 — 用户意图 → 得体的微信消息

独立模块，供 LLM tool calling 和 CLI 直接调用。
"""

from agent.protocol import ok, err
from agent.llm import chat_simple
from agent.tools._state import state

TONE_DESCRIPTIONS = {
    "warm": "温暖自然，表达关心但不肉麻",
    "casual": "轻松随意，像朋友聊天",
    "humorous": "幽默风趣，带一点俏皮",
    "sincere": "真诚直接，坦诚不绕弯",
    "concise": "简洁明了，一句话说清楚",
    "romantic": "浪漫甜蜜，适合关系升温期",
}


def express_translate(meaning: str, tone: str = "warm",
                      context_note: str = "") -> dict:
    """将用户意图翻译为得体的微信消息

    Args:
        meaning: 用户想表达的意思
        tone: 目标语气 (warm/casual/humorous/sincere/concise/romantic)
        context_note: 额外上下文

    Returns:
        {ok, data: {original, tone, versions}}
    """
    tone_desc = TONE_DESCRIPTIONS.get(tone, "自然口语")

    prompt = f"""你是文字润色助手。用户告诉你他想表达的意思，你把它改写成自然、得体的微信消息。

## 目标语气
{tone_desc}

## 改写要求
- 输出 2-3 个版本，用序号分隔
- 每个版本标注括号中的风格提示，如 (温暖版) (轻松版)
- 每个版本不超过 150 字
- 像真人聊天、自然口语化，不正式、不书面化
- 符合中国年轻人的微信聊天习惯
- 如果用户想表达的意思本身就很自然，可以微调而不是重写"""

    user_msg = f"意图: {meaning}"
    if context_note:
        user_msg += f"\n\n上下文: {context_note}"

    llm_cfg = state.config.get("llm", {})
    result = chat_simple(
        user_message=user_msg,
        system_prompt=prompt,
        config=llm_cfg,
    )

    if result.startswith("[LLM 错误]"):
        return err(result)

    return ok({
        "original": meaning,
        "tone": tone,
        "tone_description": tone_desc,
        "versions": result,
    })
