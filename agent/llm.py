"""
LLM 客户端 — OpenAI 兼容 API，支持函数调用。

用户配置 (config.yaml llm 段):
  provider: deepseek | openai | custom
  model: 模型名
  api_base: API 地址
  api_key: API 密钥
"""

import json
import time
import httpx

# ============================================================================
# 系统提示词模板（具体内容在 system_prompt.md，此处为变量替换后的组装）
# ============================================================================

SYSTEM_PROMPT_TEMPLATE = """你是 LearnLove Agent — 用户的微信聊天助手。

## 你的身份
你是用户的聊天副驾驶，运行在本地，可以直接查询微信数据库、解码语音消息、生成回复建议。
你温暖、清醒、站在用户一边。

## 核心能力
- 查看联系人列表和聊天记录
- 检测新消息（文字和语音），自动解码语音为文本
- 根据对话上下文和关系记忆，生成 1-3 条回复建议
- 将建议回复写入剪贴板（用户 Ctrl+V 即可发送）
- 管理长期记忆：记录对方关键信息、对话经验、待办事项
- 切换沟通风格和策略（通过技能系统）
- 翻译用户意图为得体的表达

## 回复生成原则
1. **先理解，再建议**：看上下文、记忆、对方性格，不要给模板化回复
2. **提供选择**：通常给 1-3 条建议，标注风格差异
3. **短小自然**：建议回复不超过 200 字，像真人聊天，不要长篇大论
4. **可操作**：用户拿到建议就能直接发（或微调后发）
5. **注意节奏**：不要每次对方发消息都给建议，判断是否需要回复、何时回复

{contact_context}

{skill_context}

{memory_context}

{lessons_context}

## 工具使用注意事项
- 查询消息前先确保解密已刷新
- 语音消息如果标记为 [语音待转]，提醒用户手动转录
- 发送功能需要相应阀门权限
- 记忆操作（record_lesson等）适度使用，不要每条消息都记录"""

# ============================================================================
# 工具定义（OpenAI Function Calling 格式）
# ============================================================================

TOOLS = [
    # ---- 联系人 ----
    {
        "type": "function",
        "function": {
            "name": "find_contact",
            "description": "查找联系人。支持按昵称、备注、微信号、别名模糊搜索。切换聊天对象前先用此工具确认联系人存在",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如联系人名称、话题关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "列出所有联系人及其基本信息（昵称、备注、最近消息时间）",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回数量上限，默认 30"},
                },
                "required": [],
            },
        },
    },
    # ---- 消息 ----
    {
        "type": "function",
        "function": {
            "name": "get_chat_history",
            "description": "获取与指定联系人的聊天记录。返回最近的消息列表（文字自动解码，语音标注类型）。支持按日期或时间范围查询",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称，输入你在 config.yaml 中配置的名称"},
                    "limit": {"type": "integer", "description": "返回消息条数，默认 30，最多 200"},
                    "date": {"type": "string", "description": "日期查询，格式灵活: '2024-08-04', '8月4日', '今天', '昨天', '前天'。自动转换为当天的 00:00 到 23:59 范围"},
                    "since_ts": {"type": "number", "description": "只返回此时间戳之后的消息（Unix timestamp）。和 before_ts 同时使用做时间范围查询"},
                    "before_ts": {"type": "number", "description": "只返回此时间戳之前的消息（Unix timestamp）。和 since_ts 同时使用做时间范围查询"},
                },
                "required": ["contact_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_new_messages",
            "description": "检查指定联系人是否有新消息。返回上次检查之后的所有新消息（文字自动解码，语音自动转录或标记待转）",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称。不指定则检查所有已配置的监听联系人"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "在所有聊天记录中搜索关键词。用于查找特定话题或历史信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "contact_name": {"type": "string", "description": "限定联系人，不指定则搜索全部"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20"},
                },
                "required": ["keyword"],
            },
        },
    },
    # ---- 语音解码 ----
    {
        "type": "function",
        "function": {
            "name": "decode_voice",
            "description": "手动解码语音消息。当某条语音消息标记为 [语音待转] 时，用此工具触发转录。需要 GPU 支持（faster-whisper）",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称"},
                    "message_ts": {"type": "number", "description": "语音消息的 create_time（Unix timestamp）"},
                },
                "required": ["contact_name", "message_ts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_voice_mode",
            "description": "切换语音处理模式。auto=自动本地转录（需要GPU），manual=标记待转等用户手动提供文本",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称"},
                    "mode": {"type": "string", "description": "auto 或 manual"},
                },
                "required": ["contact_name", "mode"],
            },
        },
    },
    # ---- 发送 ----
    {
        "type": "function",
        "function": {
            "name": "copy_reply",
            "description": "将回复文本复制到 Windows 剪贴板。用户切换到微信后 Ctrl+V 即可粘贴发送。需要阀门 L1 或以上",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要复制到剪贴板的回复文本"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "auto_send_message",
            "description": "自动发送微信消息（通过 pyautogui 操控微信窗口）。需要阀门 L2。会自动搜索联系人、粘贴文本、回车发送。使用前确认微信窗口已打开",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要发送的消息文本"},
                    "contact_name": {"type": "string", "description": "发送给谁（微信昵称或备注）"},
                },
                "required": ["text", "contact_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_wechat_window",
            "description": "检查微信窗口是否可用（是否在运行、是否可见）。用于确认自动发送的前提条件",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ---- 记忆 ----
    {
        "type": "function",
        "function": {
            "name": "view_memory",
            "description": "查看当前联系人的长期记忆（对方关键信息、关系阶段、沟通经验等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称，不指定则查看当前活跃联系人"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_lesson",
            "description": "记录一条沟通经验教训（per-contact，不会串到其他联系人）。发现有效的回复方式、对方的偏好、或踩过的坑后记录下来，后续聊天时会自动注入",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "简短标题，如「她加班时不要催促回复」"},
                    "content": {"type": "string", "description": "详细内容：什么问题、正确做法是什么"},
                    "tags": {"type": "string", "description": "逗号分隔的标签，如 '节奏, 关心'"},
                    "contact_name": {"type": "string", "description": "关联的联系人名称。默认当前联系人"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_lessons",
            "description": "列出当前联系人的经验教训",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称。默认当前联系人"},
                },
                "required": [],
            },
        },
    },
    # ---- 表达翻译 ----
    {
        "type": "function",
        "function": {
            "name": "express_translate",
            "description": "将用户的意图翻译为得体的微信消息。用户说「我想表达关心但不要太肉麻」，你调用此工具生成多个风格的版本。这不同于直接生成回复建议——此工具专注于措辞润色和风格转换",
            "parameters": {
                "type": "object",
                "properties": {
                    "meaning": {"type": "string", "description": "用户想表达的意思，如「我想告诉她我今天也很忙但一直在想她」"},
                    "tone": {"type": "string", "description": "目标语气: warm(温暖), casual(随意), humorous(幽默), sincere(真诚), concise(简洁)。默认 warm"},
                    "context_note": {"type": "string", "description": "额外上下文，如「她刚发消息说今天很累」"},
                },
                "required": ["meaning"],
            },
        },
    },
    # ---- 上下文 ----
    {
        "type": "function",
        "function": {
            "name": "get_chat_context",
            "description": "获取完整的对话上下文摘要，包括最近的消息往来、双方情绪状态、关键话题。用于快速了解当前对话状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称"},
                    "recent_turns": {"type": "integer", "description": "最近几轮对话，默认 10"},
                },
                "required": ["contact_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_feed",
            "description": "读取实时消息流（data/live/incoming.jsonl）。获取监听守护线程写入的最新消息。用于检查是否有新的未读消息",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "按发送者名称过滤，不指定则返回所有"},
                    "mark_read": {"type": "boolean", "description": "是否标记为已读，默认 true"},
                },
                "required": [],
            },
        },
    },
    # ---- 监控 ----
    {
        "type": "function",
        "function": {
            "name": "check_monitor_status",
            "description": "检查后台监听守护线程的状态（是否运行中、监听的联系人、最后检测时间）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_monitoring",
            "description": "启动后台消息监听（轮询数据库变化）。启动后新消息会自动检测并推送到消息队列",
            "parameters": {
                "type": "object",
                "properties": {
                    "contacts": {"type": "array", "items": {"type": "string"}, "description": "要监听的联系人名称列表，对应 config.yaml 中配置的名称"},
                },
                "required": ["contacts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_monitoring",
            "description": "停止后台消息监听",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ---- 风格分析 ----
    {
        "type": "function",
        "function": {
            "name": "analyze_my_style",
            "description": "从最近的聊天记录中分析你（用户自己）的语言风格——句式、语气、表情、口头禅、消息特征等。分析结果会保存并自动注入系统提示词，让 AI 回复更像你的说话方式",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_contact_style",
            "description": "分析指定联系人的语言风格——他们说法的句式、语气、常用词、互动模式等。分析结果会保存并在后续对话中自动参考，帮助你更精准地理解和回复对方",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "要分析的联系人名称"},
                },
                "required": ["contact_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_style",
            "description": "查看风格分析结果。不指定联系人则查看你自己的风格，指定联系人则查看对方的风格",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "联系人名称，不指定则查看自己的风格"},
                },
                "required": [],
            },
        },
    },
    # ---- 跨联系人查看 ----
    {
        "type": "function",
        "function": {
            "name": "peek_contact",
            "description": "查看另一个联系人的记忆、风格、教训和最近对话。不会切换当前联系人，只是参考。在跟A聊天时需要参考B的信息时使用",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "要查看的联系人名称"},
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要查看的部分: memory, style, lessons, recent_transcript。默认全部",
                    },
                },
                "required": ["contact_name"],
            },
        },
    },
    # ---- 技能管理 ----
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "列出所有可用技能（沟通风格、情境策略、军师模式等）。查看技能名称、分类和是否已激活",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_skill",
            "description": "激活一个技能。激活后该技能的提示词修饰会注入到系统提示词中，改变回复风格或策略。如激活 warm_caring（温暖关心）、humorous（幽默）、goutoujunshi（狗头军师）等",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名称，如 'warm_caring'、'humorous'、'goutoujunshi'"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deactivate_skill",
            "description": "停用一个已激活的技能",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名称"},
                },
                "required": ["name"],
            },
        },
    },
    # ---- 文件 ----
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件内容（纯文本和 Word 文档）。支持 .txt .md .json .csv .py .yaml .log .docx 等。当用户提到某个文件、文档或想让你了解文件内容时使用。最大 5MB",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的完整路径，如 'D:\\docs\\notes.txt' 或 '/home/user/doc.md'"},
                },
                "required": ["path"],
            },
        },
    },
    # ---- 系统 ----
    {
        "type": "function",
        "function": {
            "name": "view_output",
            "description": "查看溢出缓存的完整工具输出。当工具结果出现 '…[截断: … id=L3 …]' 标记时，用此工具取回完整内容。支持分页和关键词过滤",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "溢出输出 id，来自截断标记，如 'L3'"},
                    "start": {"type": "integer", "description": "起始行号 (1-based)，默认 1"},
                    "end": {"type": "integer", "description": "结束行号"},
                    "grep": {"type": "string", "description": "正则过滤，返回匹配行"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_contact",
            "description": "切换当前活跃联系人。切换后会加载该联系人的记忆和聊天历史",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string", "description": "要切换到的联系人名称"},
                },
                "required": ["contact_name"],
            },
        },
    },
]


# ============================================================================
# LLM 调用
# ============================================================================

def _get_tools():
    """合并内置工具 + SkillManager 注册的外部工具。"""
    try:
        from agent.tool_manager import skillmgr
        return TOOLS + skillmgr.get_schemas()
    except Exception:
        return TOOLS


def chat(messages, tools=None, api_base="", api_key="", model="",
         temperature=0.7, max_tokens=4096):
    """调用 OpenAI 兼容 API，支持函数调用。

    Parameters
    ----------
    messages : list[dict]
        对话历史，格式: [{"role": "system|user|assistant|tool", "content": "..."}]
    tools : list | None
        工具定义列表（OpenAI function calling 格式），None 则纯文本对话
    api_base, api_key, model : str
        LLM 连接配置
    temperature : float
        输出随机性 0.0–2.0（默认 0.7，聊天建议需适度创造性）
    max_tokens : int
        单次回复最大输出 token 数（默认 4096）

    Returns
    -------
    dict
        {"type": "text", "content": "..."}
        或 {"type": "tool_calls", "calls": [{"name": "...", "args": {...}, "id": "..."}, ...]}
        或 {"type": "error", "content": "..."}
    """
    url = api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    try:
        last_error = None
        for attempt in range(3):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0)
                ) as client:
                    resp = client.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                break
            except httpx.ReadTimeout:
                last_error = "LLM 读取超时 (300s)"
                if attempt < 2:
                    time.sleep(2 ** attempt)
            except httpx.ConnectTimeout:
                last_error = "LLM 连接超时 (15s) — 请检查网络或 API 地址"
                if attempt < 2:
                    time.sleep(2 ** attempt)
            except httpx.RemoteProtocolError:
                last_error = "LLM 连接被服务端关闭，正在重试..."
                if attempt < 2:
                    time.sleep(2 ** attempt)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (502, 503, 504) and attempt < 2:
                    last_error = f"API 错误 ({status})，正在重试..."
                    time.sleep(2 ** attempt)
                else:
                    raise
            except httpx.RequestError as e:
                last_error = f"网络错误: {e}"
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            return {"type": "error", "content": last_error or "LLM 请求失败（已重试 3 次）"}
    except httpx.HTTPStatusError as e:
        return {"type": "error", "content": f"API 错误 ({e.response.status_code}): {e.response.text[:300]}"}
    except Exception as e:
        return {"type": "error", "content": str(e)}

    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})

    # 函数调用
    if message.get("tool_calls"):
        calls = []
        for tc in message["tool_calls"]:
            func = tc.get("function", {})
            name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            calls.append({"name": name, "args": args, "id": tc.get("id", "")})
        return {"type": "tool_calls", "calls": calls}

    # 纯文本
    content = message.get("content", "")
    return {"type": "text", "content": content}


def chat_simple(user_message: str, system_prompt: str = "", config: dict = None) -> str:
    """简化的单轮对话接口 — 发送一条用户消息，返回文本回复。
    用于不需要工具调用的场景（如记忆压缩、表达翻译等）。
    """
    cfg = config or {}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    result = chat(
        messages=messages,
        api_base=cfg.get("api_base", ""),
        api_key=cfg.get("api_key", ""),
        model=cfg.get("model", ""),
        temperature=cfg.get("temperature", 0.3),
        max_tokens=cfg.get("max_tokens", 4096),
    )
    if result["type"] == "text":
        return result["content"]
    elif result["type"] == "error":
        return f"[LLM 错误] {result['content']}"
    else:
        return "[LLM 返回了工具调用，但此接口不支持]"
