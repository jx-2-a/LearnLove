"""
REPL 主循环 — Rich 终端界面 + 工具调用循环

三种模式:
  交互式 — 用户输入自然语言，Agent 调用工具并给出回复建议
  自动 (/a) — 后台监听 + 自动检测新消息 + 自动建议
  守护   — 纯后台监控 (loop.py --daemon)

工具调用循环:
  llm_chat → 检查返回值
    → tool_calls: 串行执行每个工具调用 → 追加结果 → 再次 llm_chat
    → text: 显示给用户
    → error: 显示错误
"""

import os
import sys
import json
import time
import threading
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

from agent.protocol import ok, err
from agent.llm import chat as llm_chat, chat_stream, _get_tools
from agent.context import ContextBuilder
from agent.memory import ContactMemory
from agent.tools import dispatch as tool_dispatch
from agent.tools._state import state
from agent.tools.review import get_review_tools
from agent.paths import context_json_path

# Windows GBK 控制台 + 中文/emoji 输出会崩（UnicodeEncodeError，坑 #15）：
# 控制台实际渲染 UTF-8（Windows 10+ 终端）时，强制 Python stdout/stderr 用 UTF-8，
# 否则 gbk 编不了 • 项目符号 / emoji。
if os.name == 'nt':
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass  # stdout 被重定向/替换（如 Web 模式捕获）时跳过


# ===== Rich 终端 =====

def _setup_console():
    """初始化 Rich 控制台"""
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.live import Live
        from rich.spinner import Spinner
        console = Console()
        return console
    except ImportError:
        return None


_console = _setup_console()

# 活动会话（agentweb.WebSessionClient）。Web 模式由 agent/web.py 注入；None = 终端模式。
# 所有 I/O（打印/输入/工具卡/思考块/状态行）经此分发，仿 Emisinver 的模式。
_session = None

# 网页设置面板热重载的运行时模型档案与生成参数。
# 由 web.py 的 on_setting 写入，_effective_llm_cfg 合并覆盖到每次 LLM 调用。
_RUNTIME = {}


def _log(text, level="info"):
    """发送一条系统提示，带等级（info|important|welcome|hint），Web 一段一气泡。"""
    if _session is not None:
        _session.log(text, level=level)
    else:
        _print(text)


def _md(text):
    """rich Markdown 对象 → SDK 展平为源 markdown（网页端渲染，[..] 不被当样式吃掉）。"""
    from rich.markdown import Markdown
    return Markdown(text)


def _effective_llm_cfg(llm_cfg: dict) -> dict:
    """网页设置面板热重载的值覆盖传入配置（未改时原样返回）。"""
    if not _RUNTIME:
        return llm_cfg
    cfg = dict(llm_cfg)
    for k in ("model_profile", "model", "provider", "protocol", "api_base", "api_key",
              "thinking", "temperature", "max_tokens", "reasoning_effort",
              "max_context_tokens"):
        if k in _RUNTIME and _RUNTIME[k] is not None:
            cfg[k] = _RUNTIME[k]
    return cfg


def _user_input(prompt: str):
    """终端模式读取一行用户输入（Web 模式走非阻塞 poll_guidance，不走这里）。
    EOF 返回 None，Ctrl+C 抛出交给外层处理。"""
    try:
        return input(prompt).strip()
    except EOFError:
        return None
    except KeyboardInterrupt:
        raise


def _print(text: str, style: str = ""):
    """安全打印。Web 模式下走 _session.render（info 气泡），终端模式走 Rich。
    任何编码（如 Windows GBK 控制台编不了 Rich 渲染的 •）都兜底为纯文本。"""
    if _session is not None:
        _session.render(text if text is not None else "")
        return
    try:
        if _console:
            if style:
                _console.print(text, style=style)
            else:
                _console.print(text)
        else:
            print(text)
    except UnicodeEncodeError:
        try:
            print(text)
        except UnicodeEncodeError:
            print(text.encode('ascii', errors='replace').decode('ascii'))


def _print_help():
    """打印帮助信息"""
    help_text = """\
# LearnLove Agent — 微信聊天助手

## Slash 命令

| 命令 | 说明 |
|------|------|
| `/a` | 切换自动监听模式 |
| `/c`, `/coach` | 切换咨询模式（不看聊天记录，纯讨论） |
| `/r`, `/review` | 复盘分析 — 分析最近聊天，帮你学习 |
| `/peek <名称>` | 查看其他联系人的记忆/风格 |
| `/file <路径>` | 读取本地文本文件 |
| `/copy` | 复制最后一条建议到剪贴板 |
| `/send` | 自动发送最后一条建议（需阀门 L2） |
| `/voice <auto|manual>` | 切换语音处理模式 |
| `/contact <名称>` | 切换当前联系人 |
| `/memory` | 查看当前联系人的长期记忆 |
| `/save <内容>` | 把重要内容留档（时间点快照，之后可找回来） |
| `/notes [关键词]` | 查看/搜索留档（含日期） |
| `/skills` | 查看活跃技能 |
| `/clear` | 清除对话历史 |
| `/h`, `/help` | 显示此帮助 |
| `exit`, `quit` | 退出 |

## 直接输入

你的问题或意图，如：

- "帮我看看有没有新消息"
- "她刚发消息说很累，怎么回比较好？"
- "把我想表达关心的意思翻译成消息"
- "帮我看看有没有新消息"
- "查一下和她的最近聊天"
"""
    if _session is not None:
        # Web 模式：整段作为一条 info 气泡上屏。纯文本不经 Rich 渲染，避免 • 编码问题。
        _session.log(help_text, level="info")
        return
    # 终端纯文本输出：Rich Markdown 渲染会把 `- ` 列表转成 • 项目符号，Windows GBK 控制台
    # 编不了会崩（坑 #15）。直接逐行安全打印，不引入任何编码难字符。
    for line in help_text.splitlines():
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode('ascii', errors='replace').decode('ascii'))


# ===== 工具调用循环 =====

_WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _time_context_text() -> str:
    """生成当前日期时间上下文片段，注入 system 提示词。

    三个模式（自动/咨询/复盘）都要知道「现在是什么时候」，
    避免 LLM 凭记忆瞎猜日期星期。
    """
    now = datetime.now()
    return (
        f"## 当前时间\n"
        f"{now.strftime('%Y年%m月%d日 %H:%M')} {_WEEKDAYS_CN[now.weekday()]}。"
        f"（今天是 {now.year}年{now.month}月{now.day}日，"
        f"现在是 {now.strftime('%H:%M')}）\n"
        f"涉及日期、时间、星期、节假日、时间差的判断，一律以此为准，不要凭记忆推测。"
    )


def _prepend_time_context(messages: list[dict]):
    """在消息最前插入一条「当前时间」system 消息（已注入则跳过）。"""
    for m in messages:
        if m.get("role") == "system" and "## 当前时间" in m.get("content", ""):
            return
    messages.insert(0, {"role": "system", "content": _time_context_text()})


def _execute_tool_loop(messages: list[dict], llm_cfg: dict,
                       active_skills: list[str] = None,
                       tools: list[dict] = None) -> str:
    """执行工具调用循环：LLM 返回 tool_calls → 执行 → 继续，直到返回文本。
    无轮数上限，LLM 可以持续调用工具直到产出文本回复。

    Args:
        messages: 对话消息
        llm_cfg: LLM 配置
        active_skills: 活跃技能
        tools: 工具 schema 列表。不传则用内置工具 + 技能工具

    Returns:
        LLM 最终文本回复，或错误信息

    Web 模式（_session 非 None）：
      - 流式输出 / 可折叠思考块 / 工具卡 / 状态行实时上屏（chat_stream）
      - 工具执行间隙 poll_guidance 收集引导，整批结束后统一注入（不拆散 tool_calls，防 400）
      - 打断（pop_interrupt）中止循环，未回应的 tool_call 兜底补消息
      - 重复工具调用检测：同 (name,args) 连续 5 次提醒、10 次强断，不设轮数上限
    """
    if tools is None:
        tools = _get_tools()
    # 注入当前日期时间，覆盖自动/咨询/回复跟进所有走工具循环的模式
    _prepend_time_context(messages)

    # 重复工具调用检测状态（坑④）
    _repeat_sig = None
    _repeat_count = 0
    _guidance = []        # 批循环期间收集的引导输入，整批结束后注入

    def _call_llm():
        """一次 LLM 调用。Web 模式流式上屏（思考/文本），终端模式静默。"""
        cfg = _effective_llm_cfg(llm_cfg)   # 每次调用取最新设置面板热重载值
        if _session is not None:
            return _stream_llm_call(messages, tools, cfg)
        return llm_chat(
            messages=messages,
            tools=tools,
            api_base=cfg.get("api_base", ""),
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""),
            temperature=cfg.get("temperature", 0.7),
            max_tokens=cfg.get("max_tokens", 4096),
            provider=cfg.get("provider", ""),
            thinking=cfg.get("thinking", False),
            reasoning_effort=cfg.get("reasoning_effort", ""),
            protocol=cfg.get("protocol", "openai"),
        )

    while True:
        result = _call_llm()

        if result["type"] == "text":
            return result["content"]

        elif result["type"] == "error":
            return f"[LLM 错误] {result['content']}"

        elif result["type"] == "cancelled":
            return "[系统] 已打断"

        elif result["type"] == "tool_calls":
            # 追加 assistant 消息（含 tool_calls）
            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["args"], ensure_ascii=False),
                        },
                    }
                    for c in result["calls"]
                ],
            }
            # 思考模式下工具调用轮需回传 reasoning_content（DeepSeek 要求）
            rc = result.get("reasoning_content", "")
            if rc:
                assistant_msg["reasoning_content"] = rc
            messages.append(assistant_msg)

            interrupted = False
            # 串行执行每个工具调用
            for call in result["calls"]:
                tool_name = call["name"]
                tool_args = call["args"]
                call_id = call["id"]

                # 重复调用检测：同 (name, args) 连续 5 次提醒、10 次强断（不设轮数上限）
                sig = (tool_name, json.dumps(tool_args, ensure_ascii=False, sort_keys=True))
                if sig == _repeat_sig:
                    _repeat_count += 1
                else:
                    _repeat_sig = sig
                    _repeat_count = 1
                if _repeat_count == 5:
                    _log("⚠️ 同一工具已连续调用 5 次，若无进展请停止并换思路。", level="hint")
                if _repeat_count >= 10:
                    _log("⚠️ 连续 10 次重复调用同一工具，已强制停止工具循环。", level="important")
                    # 本批未回应的 tool_call 兜底补消息，避免悬空 tool_calls（坑⑤）
                    for c in result["calls"]:
                        if not any(m.get("tool_call_id") == c["id"] for m in messages if m["role"] == "tool"):
                            messages.append({"role": "tool", "tool_call_id": c["id"],
                                             "content": "已中断，未执行（重复调用强断）"})
                    return "[系统] 检测到重复工具调用，已停止循环。请明确下一步"

                if _session is not None:
                    _session.tool_event("start", name=tool_name, args=tool_args)
                else:
                    _print(f"  🔧 {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:80]})", style="dim")

                # 执行
                try:
                    tool_result = tool_dispatch(tool_name, **tool_args)
                except Exception as e:
                    tool_result = err(f"工具执行异常: {e}")

                if _session is not None:
                    if tool_result.get("ok"):
                        _session.tool_event(
                            "end", name=tool_name, ok=True,
                            summary=json.dumps(tool_result.get("data"), ensure_ascii=False, default=str)[:2000])
                    else:
                        _session.tool_event(
                            "end", name=tool_name, ok=False,
                            error=str(tool_result.get("error", "")))

                # 格式化结果
                result_text = json.dumps(tool_result, ensure_ascii=False)
                if len(result_text) > 4000:
                    from agent.outputs import spill
                    info = spill(result_text, source=tool_name)
                    result_text = (
                        f"{{'ok': {tool_result.get('ok')}, "
                        f"'截断': '完整输出 {info['lines']} 行, id={info['id']}, "
                        f"用 view_output({info['id']}) 查看'}}"
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_text,
                })

                # 工具执行间隙：收集引导输入（只收集不打断，批结束后统一注入）；检查打断
                if _session is not None:
                    g = _session.poll_guidance()
                    if g:
                        _guidance.append(g)
                    if _session.pop_interrupt():
                        interrupted = True
                        break

            if interrupted:
                # 兜底：批次内未回应的 tool_call 补 tool 消息，避免 "tool_calls must be
                # followed by tool messages" 400（坑⑤）
                for c in result["calls"]:
                    if not any(m.get("tool_call_id") == c["id"] for m in messages if m["role"] == "tool"):
                        messages.append({"role": "tool", "tool_call_id": c["id"],
                                         "content": "已中断，未执行"})
                _log("⏹ 已打断。", level="info")
                return "[系统] 已打断"

            # 整批结束：注入收集到的引导输入（下轮 LLM 一并响应，天然不拆散 tool_calls）
            if _guidance:
                for g in _guidance:
                    messages.append({"role": "user", "content": g})
                    if _session is not None:
                        _session.user_message(g)   # 回显引导输入，前端可见
                _guidance = []


def _stream_llm_call(messages: list[dict], tools: list[dict] | None,
                     cfg: dict) -> dict:
    """Web 模式 LLM 调用：SSE 流式 → 思考块 / 文本增量实时上屏。

    返回与 llm.chat() 同形状的 result dict：
      {"type": "text", "content", "reasoning_content"}
      {"type": "tool_calls", "calls", "reasoning_content"}
      {"type": "error", "content"}
      {"type": "cancelled"}
    """
    _session.set_status("思考中...")
    result = None
    content_parts = []
    try:
        for ev in chat_stream(
            messages, tools=tools,
            api_base=cfg.get("api_base", ""),
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""),
            temperature=cfg.get("temperature", 0.7),
            max_tokens=cfg.get("max_tokens", 4096),
            provider=cfg.get("provider", ""),
            thinking=cfg.get("thinking", False),
            reasoning_effort=cfg.get("reasoning_effort", ""),
            cancel=lambda: _session.pop_interrupt(),
            protocol=cfg.get("protocol", "openai"),
        ):
            t = ev["type"]
            if t == "delta":
                content_parts.append(ev["content"])
                _session.stream_delta(ev["content"])
            elif t == "reasoning":
                _session.thinking_delta(ev["content"])
            elif t == "tool_calls":
                _session.thinking_end()      # 思考结束，关闭思考块再执行工具
                result = ev
                preamble = "".join(content_parts).strip()
                if preamble:
                    # 工具轮里 LLM 先说的正文 → markdown 收尾成一条消息
                    _session.stream_end(_md(preamble))
                else:
                    _session.stream_end(None)   # 无正文 → 不产出消息
            elif t == "done":
                _session.thinking_end()
                content = ev.get("content") or ""
                if content:
                    _session.stream_end(_md(content))
                else:
                    _session.stream_end(None)
                result = {"type": "text", "content": content,
                          "reasoning_content": ev.get("reasoning_content", "")}
            elif t == "error":
                _session.thinking_end()
                result = ev
                _session.stream_end(None)
            elif t == "cancelled":
                _session.thinking_end()
                result = ev
                _session.stream_end(None)
            if result is not None:
                break
    finally:
        _session.set_status("")
    if result is None:
        result = {"type": "error", "content": "LLM 流式响应为空"}
    return result


# ===== 自动模式 =====

def _check_interrupt() -> bool:
    """检查用户是否按下 Enter 打断"""
    if os.name == 'nt':
        import msvcrt
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b'\r', b'\n'):
                return True
    return False


def _drain_monitor_queue() -> list[dict]:
    """排空监控队列，返回所有待处理消息"""
    entries = []
    if state.monitor_running:
        try:
            while True:
                entries.append(state.monitor_queue.get_nowait())
        except Exception:
            pass
    return entries


def _get_skill_modifiers() -> str:
    """获取所有活跃技能的 prompt_modifier 拼接"""
    try:
        from agent.tool_manager import skillmgr
        return skillmgr.get_prompt_modifiers()
    except Exception:
        return ""


# ===== 主 REPL =====

def run_chat(config: dict, session=None):
    """主 REPL 入口

    1. 初始化状态（加载联系人、设置活跃联系人）
    2. 加载记忆
    3. 显示欢迎信息
    4. 进入交互循环

    session : agentweb.WebSessionClient | None
        Web 模式传入（由 agent/web.py 注入）；None = 终端模式。
        注入后本函数所有 I/O 走网页（气泡/输入栏/工具卡/思考块）。
    """
    global _session
    _session = session
    llm_cfg = config.get("llm", {})
    contacts_cfg = config.get("contacts", [])
    memory_cfg = config.get("memory", {})
    agent_cfg = config.get("agent", {})
    valve_cfg = config.get("valve", {})
    active_skills = config.get("active_skills", [])

    # 设置阀门
    from agent.valve import ValveLevel, set_valve
    level = valve_cfg.get("level", 1)
    set_valve(ValveLevel(level))
    level_names = {0: "只读", 1: "建议+剪贴板", 2: "自动发送"}

    # 初始化上下文构建器
    ctx_builder = ContextBuilder(config)

    # 设置活跃联系人
    contact_name = ""
    contact_wxid = ""
    contact_memory = None

    if contacts_cfg:
        first = contacts_cfg[0]
        contact_wxid = first.get("wxid", "")
        contact_name = first.get("name", "")
        state.active_contact_wxid = contact_wxid
        state.active_contact_name = contact_name

        # 加载记忆
        contact_memory = ContactMemory(contact_name, llm_cfg)
        contact_memory.startup()

    # 标题
    _print("")
    _print("  🤖 LearnLove Agent — 微信聊天助手", style="bold cyan")
    _print(f"  联系人: {contact_name or '(未设置)'}  阀门: {level_names.get(level, level)}  技能: {', '.join(active_skills) or '无'}")
    _print(f"  输入 /h 查看命令  /a 自动监听  /c 咨询模式  /r 复盘")
    _print("")

    # 对话消息历史
    chat_messages = []
    last_suggestion = ""
    coach_mode = False  # 咨询模式：不看聊天记录，纯讨论
    review_context = ""  # 复盘分析结果，切入咨询模式时注入

    # 辅助：保存/加载会话上下文（agent <-> 用户对话，非微信消息）
    def _save_context(name: str, msgs: list, keep: int = 40):
        """保存最近 N 条对话到磁盘，用于重启恢复。"""
        if not name or not msgs:
            return
        try:
            p = context_json_path(name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(msgs[-keep:], f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 保存失败不影响运行

    def _load_context(name: str) -> list:
        """从磁盘恢复对话历史。"""
        try:
            p = context_json_path(name)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
                if isinstance(msgs, list):
                    return msgs
        except Exception:
            pass
        return []

    # 启动时恢复上次的对话上下文
    if contact_name:
        saved = _load_context(contact_name)
        if saved:
            chat_messages = saved
            _print(f"  📝 已恢复上次对话上下文 ({len(saved)} 条消息)", style="dim")

    while True:
        try:
            # 排空监控队列
            auto_new = _drain_monitor_queue()

            # 处理自动模式的新消息 — 批量处理，一次 LLM 调用
            if auto_new:
                # === Step 1: 预处理（语音转录、联系人解析） ===
                processed = []
                primary_contact_name = contact_name
                primary_contact_wxid = contact_wxid

                for entry in auto_new:
                    entry_contact = entry.get("sender", "?")
                    entry_content = entry.get("content", "")
                    entry_type = entry.get("type", 1)

                    # 自己的消息：显示但不分析，作为上下文保留
                    is_self = (entry_contact == "你")

                    # 语音消息处理
                    if entry_type == 34:
                        entry_wxid = entry.get("wxid", "")
                        voice_cfg = state.voice_mode(entry_wxid)
                        if voice_cfg == "manual":
                            _print("  🎤 语音消息 — 手动模式，跳过", style="yellow")
                            continue
                        else:
                            _print("  🎤 正在转录语音...", style="dim")
                            try:
                                from agent.tools.decode import decode_voice
                                vresult = decode_voice(contact_name=entry_contact, message_ts=entry.get("create_time", 0))
                                if vresult.get("ok"):
                                    entry_content = vresult["data"].get("text", entry_content)
                                    _print(f"  🎤 转录: {entry_content[:100]}", style="dim")
                                else:
                                    _print(f"  🎤 转录失败: {vresult.get('error', '未知')}", style="red")
                                    continue
                            except Exception as e:
                                _print(f"  🎤 转录异常: {e}", style="red")
                                continue

                    # 确定联系人
                    msg_wxid = entry.get("wxid", "")
                    msg_contact_name = state.contacts.get(msg_wxid, {}).get("display", entry_contact)

                    if msg_contact_name and msg_contact_name != primary_contact_name:
                        if primary_contact_name == contact_name:
                            # 首次遇到不同联系人，切换
                            _print(f"  🔄 自动切换联系人: {primary_contact_name} → {msg_contact_name}", style="dim")
                            primary_contact_name = msg_contact_name
                            primary_contact_wxid = msg_wxid
                            contact_name = msg_contact_name
                            contact_wxid = msg_wxid
                            contact_memory = ContactMemory(contact_name, llm_cfg)
                            contact_memory.startup()

                    # 打印消息（自己的和对方的用不同标记）
                    if is_self:
                        _print(f"  📤 [你] {entry_content[:80]}", style="dim")
                    else:
                        _print(f"\n  📩 [自动] {entry_contact} ({entry.get('time', '')}): {entry_content[:100]}", style="yellow")

                    processed.append({
                        "sender": entry_contact,
                        "content": entry_content,
                        "time": entry.get("time", ""),
                        "is_self": is_self,
                    })

                if not processed:
                    continue

                # 如果全是自己的消息，不触发分析（只作为上下文记住）
                from_contact = [m for m in processed if not m.get("is_self")]
                if not from_contact:
                    continue

                # === Step 2: 批量构建上下文 + LLM 调用（带「偷跑检测」） ===
                _print(f"  ▸ 批量处理 {len(processed)} 条消息（{len(from_contact)} 条来自对方）...", style="dim")

                reply = None
                max_retries = 2  # 最多追加两轮，防止对方刷屏死循环

                for retry in range(max_retries + 1):
                    msg_context = ctx_builder.build_context(
                        contact_name=contact_name,
                        contact_wxid=contact_wxid,
                        new_messages=processed,
                        memory_text=contact_memory.memory_text() if contact_memory else "",
                        skill_modifiers=_get_skill_modifiers(),
                        max_tokens=agent_cfg.get("context", {}).get("max_context_tokens", 8000),
                    )

                    with _console.status("[cyan]思考中...[/cyan]") if _console else contextlib.nullcontext():
                        reply = _execute_tool_loop(msg_context, llm_cfg, active_skills)

                    # 检查 LLM 思考期间有没有新消息偷跑进来
                    late_entries = _drain_monitor_queue()
                    if not late_entries:
                        break  # 没有新消息，直接输出

                    # 有新消息！预处理后追加到批次
                    late_processed = []
                    for entry in late_entries:
                        entry_contact = entry.get("sender", "?")
                        entry_content = entry.get("content", "")
                        entry_type = entry.get("type", 1)
                        is_self = (entry_contact == "你")

                        # 自己的消息只静默记录
                        if is_self:
                            _print(f"  📤 [偷跑·你] {entry_content[:60]}", style="dim")
                            late_processed.append({
                                "sender": entry_contact,
                                "content": entry_content,
                                "time": entry.get("time", ""),
                                "is_self": True,
                            })
                            continue

                        # 语音跳过（偷跑消息的语音暂不处理，避免阻塞）
                        if entry_type == 34:
                            _print(f"  🎤 [偷跑] 语音消息，跳过转录", style="yellow")
                            continue

                        _print(f"  ⚡ [偷跑] {entry_contact} ({entry.get('time', '')}): {entry_content[:80]}", style="yellow")
                        late_processed.append({
                            "sender": entry_contact,
                            "content": entry_content,
                            "time": entry.get("time", ""),
                            "is_self": False,
                        })

                    if late_processed:
                        processed.extend(late_processed)
                        # 只有对方发了新消息才重新分析
                        late_from_contact = [m for m in late_processed if not m.get("is_self")]
                        if late_from_contact:
                            _print(f"  🔄 思考中到达 {len(late_from_contact)} 条，合并重分析（第 {retry + 1} 次）...", style="dim")
                            continue
                        else:
                            break  # 只有自己的消息，不重分析
                    else:
                        break  # 全是语音且跳过了

                if reply and not reply.startswith("[系统]"):
                    last_suggestion = reply
                    # Web 模式回复已由 _execute_tool_loop 流式上屏（assistant 气泡），不重复打印
                    if _session is None:
                        _print(f"\n  🤖 建议回复:", style="bold green")
                        _print(f"  {reply}", style="green")

                    # 自动复制到剪贴板
                    if agent_cfg.get("auto", {}).get("auto_copy", False):
                        try:
                            from agent.tools.send import copy_to_clipboard
                            copy_to_clipboard(reply)
                            _print("  📋 已复制到剪贴板", style="dim")
                        except Exception:
                            pass

                    # 记录记忆（一次性记录所有消息 + 回复）
                    if contact_memory:
                        all_incoming = "\n".join(
                            f"[{m['time']}] {m['sender']}: {m['content']}"
                            for m in processed
                        )
                        contact_memory.log_turn(
                            incoming_msg=all_incoming,
                            incoming_sender=primary_contact_name,
                            suggested_reply=reply,
                        )

                        if contact_memory.needs_compact(
                            max_chars=memory_cfg.get("transcript_max_chars", 60000),
                            max_turns=memory_cfg.get("compact_turns", 30),
                        ):
                            _print("  📝 压缩记忆中...", style="dim")
                            contact_memory.compact(
                                max_memory_chars=memory_cfg.get("max_memory_chars", 4000),
                            )

                continue  # 处理完自动新消息后继续循环

            # 获取用户输入
            prompt = "咨询 > " if coach_mode else "你 > "
            if _session is not None:
                # Web 模式：非阻塞轮询引导输入（输入栏随时可提交），不阻塞监控队列。
                # pop_interrupt 消费打断标志——打断在流式/工具循环里已消费，这里兜底防残留，
                # 避免点「打断」误杀整个会话（区别于终端 EOF）。
                _session.pop_interrupt()
                if state.monitor_queue and not state.monitor_queue.empty():
                    continue          # 有新消息 → 回到顶部 drain+处理
                user_input = _session.poll_guidance()
                if user_input is None:
                    time.sleep(0.5)   # 稍睡避免空转
                    continue
                # 回显用户输入：网页输入栏提交后不留痕，须补一笔 user 气泡（可重放，刷新后可见）
                _session.user_message(user_input)
            elif state.monitor_running:
                # 自动模式：轮询等待输入，同时不阻塞监控队列
                # 用 \r 保持在同行刷新，不堆空行
                import msvcrt
                user_input = None
                p = prompt
                for i in range(40):  # 最多等 20 秒（40 × 0.5s）
                    if i == 0:
                        sys.stdout.write(f"\r{p}")
                        sys.stdout.flush()
                    if msvcrt.kbhit():
                        user_input = input("").strip()
                        break
                    time.sleep(0.5)
                    # 检查队列是否有新消息
                    try:
                        if state.monitor_queue and not state.monitor_queue.empty():
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            break
                    except Exception:
                        pass
                    # 每轮刷新提示符（\r 回到行首，保持在同一行）
                    if i > 0 and i % 4 == 0:  # 每 2 秒刷新一次
                        sys.stdout.write(f"\r{p}")
                        sys.stdout.flush()
                else:
                    # 超时无输入无新消息 → 回到顶部（\r 清掉提示符）
                    sys.stdout.write("\r")
                    sys.stdout.flush()
                    continue
                # 因为新消息退出 → 回到循环顶部 drain+处理
                if user_input is None:
                    continue
            else:
                user_input = _user_input(prompt)
                if user_input is None:
                    break  # 终端 EOF

            if not user_input:
                continue

            # Slash 命令
            if user_input.startswith("/"):
                result = _handle_slash(user_input, config, contact_name, contact_wxid,
                                       contact_memory, last_suggestion)
                if result == "exit":
                    break
                if isinstance(result, tuple) and result[0] == "switch_contact":
                    new_name = result[1]
                    if new_name:
                        # 保存旧联系人的对话上下文
                        if contact_name and chat_messages:
                            _save_context(contact_name, chat_messages)
                        # 切换
                        contact_name = new_name
                        contact_wxid = state.active_contact_wxid
                        contact_memory = ContactMemory(contact_name, llm_cfg)
                        contact_memory.startup()
                        # 加载新联系人的对话上下文
                        loaded = _load_context(contact_name)
                        chat_messages = loaded
                        _print(f"  已切换到: {contact_name}", style="green")
                        if loaded:
                            _print(f"  📝 已恢复对话上下文 ({len(loaded)} 条消息)", style="dim")
                elif result == "review":
                    analysis_text = _do_review(contact_name, contact_wxid, llm_cfg, config,
                                               contact_memory=contact_memory)
                    # 复盘完成后自动切咨询模式，方便讨论分析结果
                    if not coach_mode:
                        if contact_name and chat_messages:
                            _save_context(contact_name, chat_messages)
                        coach_mode = True
                        if analysis_text:
                            review_context = analysis_text
                        _print("  💬 已自动切换到咨询模式 — 可以直接讨论复盘内容", style="cyan")
                        chat_messages = []
                elif result == "toggle_coach":
                    coach_mode = not coach_mode
                    if coach_mode:
                        # 保存当前上下文再清空，方便复盘
                        if contact_name and chat_messages:
                            _save_context(contact_name, chat_messages)
                        _print("  💬 咨询模式已开启 — 不看聊天记录，纯讨论分析", style="cyan")
                        chat_messages = []
                    else:
                        _print("  📝 已回到回复跟进模式", style="cyan")
                continue

            if user_input.lower() in ("exit", "quit"):
                break

            # 普通对话：发送给 LLM
            _print("  ▸ 思考中...", style="dim")

            # 构建上下文（咨询模式用不同的 system prompt）
            if coach_mode:
                # 咨询模式：COACH_SYSTEM_PROMPT + 技能 + 记忆（无聊天历史）
                msg_context = [{"role": "system", "content": COACH_SYSTEM_PROMPT}]
                # 注入技能上下文（狗头军师等）
                skill_mod = _get_skill_modifiers()
                if skill_mod:
                    msg_context.append({"role": "system", "content": skill_mod})
                # 注入复盘上下文（从 /review 切过来的）
                if review_context:
                    msg_context.append({
                        "role": "system",
                        "content": (
                            f"## ⚠️ 重要：以下是刚才对 {contact_name} 的复盘分析结果\n\n"
                            f"{review_context}\n\n"
                            f"---\n"
                            f"请基于以上复盘分析结果与用户展开讨论。用户可能会问复盘中的具体问题、"
                            f"想深入某个建议、或讨论后续怎么做。主动引导用户思考复盘中最关键的发现，"
                            f"帮他落实到具体行动上。不要只是复述复盘内容——要像朋友聊天一样自然地讨论。"
                        ),
                    })
                # 注入记忆和风格
                mem = contact_memory.memory_text() if contact_memory else ""
                if mem:
                    msg_context.append({"role": "system", "content": f"## 关于 {contact_name}\n{mem}"})
                from agent.style_profiler import load_user_style, load_contact_style
                ustyle = load_user_style()
                cstyle = load_contact_style(contact_name) if contact_name else ""
                if ustyle or cstyle:
                    style_text = ""
                    if ustyle:
                        style_text += f"## 你的风格\n{ustyle[:400]}\n\n"
                    if cstyle:
                        style_text += f"## {contact_name} 的风格\n{cstyle[:300]}"
                    if style_text:
                        msg_context.append({"role": "system", "content": style_text})
                # 追加对话历史（仅本轮 session 的，不含聊天记录）
                for cm in chat_messages[-10:]:
                    msg_context.append(cm)
                msg_context.append({"role": "user", "content": user_input})
                # 咨询模式额外暴露复盘报告工具，便于回顾历史复盘
                active_tools = _get_tools() + get_review_tools()
            else:
                # 回复模式：完整上下文
                msg_context = ctx_builder.build_context(
                    contact_name=contact_name,
                    contact_wxid=contact_wxid,
                    recent_history=None,
                    memory_text=contact_memory.memory_text() if contact_memory else "",
                    skill_modifiers=_get_skill_modifiers(),
                    max_tokens=agent_cfg.get("context", {}).get("max_context_tokens", 8000),
                )
                msg_context.append({"role": "user", "content": user_input})
                active_tools = None  # 用默认工具集（不含复盘报告工具）

            # LLM 工具调用循环
            with _console.status("[cyan]思考中...[/cyan]") if _console else contextlib.nullcontext():
                reply = _execute_tool_loop(msg_context, llm_cfg, active_skills, tools=active_tools)

            if reply:
                if reply.startswith("[LLM 错误]") or reply.startswith("[系统]"):
                    _log(f"❌ {reply}", level="important")
                else:
                    last_suggestion = reply
                    # Web 模式回复已由 _execute_tool_loop 流式上屏，不重复打印
                    if _session is None:
                        _print(f"\n  🤖 {reply}\n", style="green")

            # 更新聊天消息
            chat_messages.append({"role": "user", "content": user_input})
            chat_messages.append({"role": "assistant", "content": reply})

            # 保持消息历史在合理长度
            max_history = agent_cfg.get("context", {}).get("max_history_turns", 20) * 2
            if len(chat_messages) > max_history:
                chat_messages = chat_messages[-max_history:]

            # 自动保存对话上下文（防崩溃丢数据）
            if contact_name:
                _save_context(contact_name, chat_messages)

            # 记录 flush
            if contact_memory:
                contact_memory.flush([
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": reply},
                ])

        except KeyboardInterrupt:
            _print("\n  按 Ctrl+C 再次或输入 exit 退出", style="dim")
        except EOFError:
            break
        except Exception as e:
            _print(f"  ❌ 错误: {e}", style="red")

    # 退出：仅在确实需要时压缩
    if contact_memory and contact_memory.needs_compact(
        max_chars=memory_cfg.get("transcript_max_chars", 60000),
        max_turns=memory_cfg.get("compact_turns", 30),
    ):
        try:
            _print("  📝 最终压缩记忆...", style="dim")
            contact_memory.compact(
                max_memory_chars=memory_cfg.get("max_memory_chars", 4000),
            )
        except Exception:
            pass

    if state.monitor_running:
        state.monitor_running = False

    # 保存会话上下文，下次启动自动恢复
    if contact_name and chat_messages:
        _save_context(contact_name, chat_messages)

    _session = None   # 归还全局：本次会话的 _session 生命周期到此结束
    _print("  👋 再见!", style="cyan")


# ===== 咨询模式 =====

COACH_SYSTEM_PROMPT = """你是 LearnLove Agent 的咨询教练模式。你不是在帮用户回消息，而是在和他讨论、分析、给建议。

## 你的角色
- 一个真诚、有洞察力的朋友 + 关系顾问
- 帮用户理清思路、看清局面、找到方向
- 可以探讨任何话题：关系、沟通、自我成长、情感困惑

## 工作方式
- 先理解用户的问题和处境，不要急着给建议
- 帮用户发现他自己没注意到的角度
- 给具体可操作的建议，不说空话
- 可以质疑用户的想法，但保持尊重
- 引用具体例子来说明观点

## 工具与沉淀
- 需要回顾历史复盘时，用 list_reviews 查看有哪些报告，read_review 读取内容
- 讨论出对后续沟通有长期价值的原则、教训、行动方案时，用 record_lesson 记录（记得带 contact_name）。会自动沉淀到 lessons.json，后续聊天自动参考
- 讨论中产生的重要内容（故事/感受/想法/做法/一起创造的东西）想留档时，用 record_note 记录（带 contact_name，无活动联系人会落「自己」）。note 是时间点快照，不被自动注入——用户想找回过去时用 view_notes 按需读取
- 不要为记录而记录，只记真正有价值的发现

## 重要原则
- 用户利益优先：情绪稳定、自尊、边界、长期幸福
- 不教用户操控别人，教他理解和沟通
- 不确定的事情，帮用户设计"小实验"去验证
- 鼓励用户自己思考和决定，而不是依赖你

## 风格
- 自然、有温度、像朋友聊天
- 可以反问、可以幽默、可以认真
- 长短适中，看问题复杂度"""


# ===== 复盘分析 =====

REVIEW_SYSTEM_PROMPT = """你是一个专业的聊天复盘教练。你的任务不是给回复建议，而是分析已经发生的对话，帮用户学习和成长。

## 全局视角（最重要的原则）
- 不要只盯眼前几句聊天。把这段对话放进「全局状态」里看——长期记忆、经验教训、关系阶段、双方风格，这些都是背景
- 判断：这是新的发展，还是旧问题的延续？与过往模式一致还是出现了矛盾/变化？
- 如果全局背景和当前对话有出入（比如对方行为变了、记忆过期了），明确指出
- 复盘的价值在于「看懂全局」，而不是逐句点评

## 工具使用（按需读取全局背景，不要一次全读）
分析前如果觉得背景不足，调用对应工具补充，别凭空猜：
- view_memory：读取长期记忆全文（关系阶段、对方信息、待办事项）
- list_lessons：读取历史沉淀的沟通经验教训
- view_style：读取双方语言风格
- get_chat_history：拉取更多聊天记录
- list_reviews / read_review：查看历史复盘报告
已有「关系快照」和「上文回顾」时，用它们做基本盘，工具只补不足的细节

## 分析框架
1. **对话脉络**: 用 2-3 句概括这段对话的走向和关键转折
2. **对方状态**: 从对方的消息中推断 TA 的情绪、需求、潜台词
3. **你的表现**:
   - ✅ 做得好的（具体指出哪句话/哪个回应好，为什么好）
   - ⚠️ 可以改进的（具体指出哪句话/哪个回应可以更好，怎么改）
4. **关键教训**: 从这段对话中能学到什么？1-3 条 actionable 的建议
5. **后续建议**: 接下来 1-3 天怎么跟进？该主动还是该等？

## 风格要求
- 直接、有用、不讨好
- 用具体消息举例，不要泛泛而谈
- 如果用户明显犯错，温和但明确地指出
- 如果用户做得好，具体说明哪里好
- 关注"用户能控制的事"，不纠结对方怎么想
- 如果提供了「上文回顾」，注意前后关联，指出变化和趋势

## 输出格式
用清晰的分段，每段有小标题。总长度不超过 600 字。"""


def _load_review_state(contact_name: str) -> dict | None:
    """加载复盘进度文件。不存在或损坏返回 None。"""
    from agent.paths import review_state_path
    path = review_state_path(contact_name)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None


def _save_review_state(contact_name: str, state: dict):
    """保存复盘进度文件"""
    from agent.paths import review_state_path
    path = review_state_path(contact_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _build_review_snapshot(contact_name: str, contact_memory=None) -> str:
    """从长期记忆提取精简的「关系快照」作为复盘的基本盘。

    只取记忆开头（关系阶段 + 对方关键信息的前半），
    完整内容不注入——让 agent 在分析时用工具按需读取。
    """
    mem = contact_memory.memory_text() if contact_memory else ""
    if not mem:
        try:
            from agent.paths import memory_md_path
            p = memory_md_path(contact_name)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    mem = f.read()
        except Exception:
            pass
    if not mem or not mem.strip():
        return ""

    return (
        f"\n\n## 关系快照（来自长期记忆的开头，完整内容用 view_memory 按需读取）\n"
        f"{mem[:1000]}\n"
    )


def _do_review(contact_name: str, contact_wxid: str, llm_cfg: dict, config: dict,
               contact_memory=None) -> str | None:
    """复盘分析：基于时间戳增量拉取聊天记录，用教练视角分析对话，帮用户学习成长。

    Args:
        contact_name: 联系人名称
        contact_wxid: 联系人 wxid
        llm_cfg: LLM 配置
        config: 全局配置
        contact_memory: 当前联系人的 ContactMemory 实例（可选，用于注入长期记忆）

    Returns:
        分析文本，失败返回 None
    """
    from agent.tools.message import get_chat_history
    import time as _time

    _print(f"\n  🔍 正在调取 {contact_name} 的聊天记录...", style="cyan")

    # ---- 1. 读取配置 ----
    review_cfg = config.get("review", {})
    initial_limit = review_cfg.get("initial_message_limit", 100)
    include_prev_summary = review_cfg.get("include_previous_summary", True)

    # ---- 2. 读取复盘进度 ----
    prev_state = _load_review_state(contact_name)
    last_review_ts = prev_state.get("last_review_ts", None) if prev_state else None
    prev_summary = prev_state.get("last_summary", None) if prev_state else None
    total_reviews = prev_state.get("total_reviews", 0) if prev_state else 0

    # ---- 3. 根据是否有历史记录决定查询策略 ----
    include_oldest = review_cfg.get("include_oldest", True)
    oldest_limit = review_cfg.get("oldest_message_limit", 50)

    early_messages = []
    if last_review_ts is not None:
        result = get_chat_history(
            contact_name=contact_name,
            since_ts=last_review_ts,
            before_ts=_time.time(),
            limit=initial_limit,
        )
        _print(f"  📍 增量分析：自上次复盘后", style="dim")
    else:
        result = get_chat_history(contact_name=contact_name, limit=initial_limit)
        _print(f"  📍 首次分析：拉取最近 {initial_limit} 条", style="dim")
        # 首次复盘额外拉取最早的一批消息，覆盖"相识初期"
        if include_oldest:
            early = get_chat_history(contact_name=contact_name, oldest=True, limit=oldest_limit)
            if early.get("ok"):
                early_messages = early["data"].get("messages", [])
            if early_messages:
                _print(f"  📍 首次分析：另拉取最早 {len(early_messages)} 条（相识初期）", style="dim")

    if not result.get("ok"):
        _print(f"  ❌ 获取聊天记录失败: {result.get('error', '未知错误')}", style="red")
        return None

    messages = result["data"].get("messages", [])
    if not messages:
        if total_reviews > 0:
            _print(f"  📭 {contact_name} 暂无新消息（上次复盘后没有新的聊天记录）", style="yellow")
        else:
            _print(f"  📭 暂无 {contact_name} 的聊天记录", style="yellow")
        return None

    # ---- 4. 合并早期与最近记录（按 create_time 去重），时间正序 ----
    early_ts = {m["create_time"] for m in early_messages}
    by_ts = {}
    for m in messages:
        by_ts[m["create_time"]] = m
    for m in early_messages:
        by_ts.setdefault(m["create_time"], m)
    messages_asc = sorted(by_ts.values(), key=lambda m: m["create_time"])
    min_ts = messages_asc[0]["create_time"]
    max_ts = messages_asc[-1]["create_time"]

    # ---- 5. 检测是否达到拉取上限（按最近段判断） ----
    hit_limit = len(messages) >= min(initial_limit, 200)

    # ---- 6. 格式化聊天记录 ----
    now_year = datetime.now().year

    def _fmt_review_time(ts):
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M") if dt.year != now_year else dt.strftime("%m-%d %H:%M")

    lines = []
    early_started = False
    recent_started = False
    for m in messages_asc:
        is_early = m["create_time"] in early_ts
        if is_early and not early_started:
            lines.append("──── 早期记录（相识初期）────")
            early_started = True
        elif not is_early and early_started and not recent_started:
            lines.append("──── 最近记录 ────")
            recent_started = True
        sender = m.get("sender", "?")
        content = m.get("content", "")
        t = m.get("time", "")
        msg_type = m.get("type", "")
        if len(content) > 500:
            content = content[:500] + "..."
        type_tag = f" [{msg_type}]" if msg_type and msg_type != "文本" else ""
        lines.append(f"[{t}] {sender}{type_tag}: {content}")

    chat_text = "\n".join(lines)
    time_range = f"{_fmt_review_time(min_ts)} ~ {_fmt_review_time(max_ts)}"
    _print(f"  📊 已加载 {len(messages_asc)} 条消息（{time_range}，含早期 {len(early_messages)} 条），正在分析...", style="cyan")

    # ---- 7. 构建 user_prompt ----
    user_prompt = (
        f"## 联系人: {contact_name}\n"
        f"## 时间范围: {time_range}\n"
        f"## 聊天记录（共 {len(messages_asc)} 条，其中「早期记录」为最早期的一批消息，用于了解相识阶段）\n"
        f"{chat_text}\n\n"
        f"请对以上对话进行复盘分析。"
    )

    # ---- 8. 加载技能上下文（保留现有逻辑） ----
    skill_context = ""
    try:
        from agent.tool_manager import skillmgr
        active = skillmgr.list_active()
        if active:
            modifiers = []
            for s in active:
                mod = skillmgr.get_prompt_modifier(s)
                if mod:
                    modifiers.append(mod)
            if modifiers:
                skill_context = "\n\n## 参考知识\n" + "\n\n".join(modifiers[:2])
    except Exception:
        pass

    # ---- 9. 注入上次复盘摘要作为"上文回顾" ----
    previous_context = ""
    if include_prev_summary and prev_summary:
        previous_context = (
            f"\n\n## 上文回顾（第 {total_reviews} 次复盘摘要）\n"
            f"{prev_summary[:800]}\n"
            f"(以上为上次复盘的关键结论，完整报告可用 read_review 按需读取)"
        )

    # ---- 9.5 注入精简的关系快照（完整内容让 agent 用工具按需读取）----
    snapshot_context = _build_review_snapshot(contact_name, contact_memory)

    full_system = (
        REVIEW_SYSTEM_PROMPT
        + snapshot_context
        + previous_context
        + skill_context
    )

    # ---- 10. 走工具循环分析（agent 可按需调用工具补充全局背景）----
    _print("  ▸ 分析中...", style="dim")
    try:
        msg_context = [{"role": "system", "content": full_system}]
        msg_context.append({"role": "user", "content": user_prompt})
        # 完整工具集：记忆/教训/风格/复盘报告/聊天记录均可按需读取
        analysis = _execute_tool_loop(
            msg_context,
            llm_cfg,
            tools=_get_tools() + get_review_tools(),
        )
    except Exception as e:
        _print(f"  ❌ 分析失败: {e}", style="red")
        return None

    if not analysis or analysis.startswith("[LLM 错误]"):
        _print(f"  ❌ 分析失败: {analysis}", style="red")
        return None

    # ---- 11. 输出分析结果 ----
    if _session is not None:
        # Web 模式：分析正文已由 _execute_tool_loop 流式上屏，这里只补一条汇总头
        _log(
            f"📊 {contact_name} — 对话复盘分析  📅 {time_range}"
            + (f"  （第 {total_reviews + 1} 次复盘）" if total_reviews > 0 else ""),
            level="important",
        )
    else:
        _print(f"\n{'─' * 50}", style="dim")
        _print(f"  📊 {contact_name} — 对话复盘分析", style="bold cyan")
        _print(f"  📅 {time_range}", style="dim")
        if total_reviews > 0:
            _print(f"  📝 第 {total_reviews + 1} 次复盘", style="dim")
        _print(f"{'─' * 50}", style="dim")
        _print(analysis)
        _print(f"{'─' * 50}\n", style="dim")

    # ---- 12. 保存复盘进度 ----
    new_state = {
        "last_review_ts": max_ts,
        "last_review_time": datetime.now().isoformat(),
        "total_reviews": total_reviews + 1,
        "last_summary": analysis[:3000],
    }
    _save_review_state(contact_name, new_state)

    if hit_limit:
        _print(f"  ⚠️ 消息数量达到单次分析上限（{min(initial_limit, 200)} 条），更早的消息可能未被覆盖。"
               f"建议提高复盘频率或增大 initial_message_limit。", style="yellow")

    # ---- 13. 保存复盘报告 ----
    try:
        from agent.paths import reviews_dir
        review_dir = reviews_dir()
        os.makedirs(review_dir, exist_ok=True)
        review_file = os.path.join(
            review_dir,
            f"{contact_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )
        with open(review_file, "w", encoding="utf-8") as f:
            f.write(f"# {contact_name} 复盘分析 — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(f"- **时间范围**: {time_range}\n")
            f.write(f"- **消息数**: {len(messages)}\n")
            f.write(f"- **第 {new_state['total_reviews']} 次复盘**\n")
            if last_review_ts:
                f.write(f"- **增量模式**: 是（since {datetime.fromtimestamp(last_review_ts).strftime('%Y-%m-%d %H:%M')}）\n")
            f.write(f"\n{analysis}")
            f.write(f"\n\n---\n原始消息数: {len(messages)}")
        _print(f"  💾 已保存: {review_file}", style="dim")
    except Exception:
        pass

    return analysis


def _handle_slash(command: str, config: dict, contact_name: str,
                  contact_wxid: str, contact_memory, last_suggestion: str) -> str:
    """处理 slash 命令。返回 "exit" 退出，"switch_contact" 切换，或 None 继续。"""
    parts = command.split()
    cmd = parts[0].lower()

    if cmd in ("/h", "/help", "/?"):
        _print_help()

    elif cmd == "/a":
        # 切换自动监听
        if state.monitor_running:
            state.monitor_running = False
            _print("  ⏸️  自动监听已关闭", style="yellow")
        else:
            if not state.monitor_thread or not state.monitor_thread.is_alive():
                from agent.tools.monitor import _start_monitoring_raw
                contacts_to_monitor = [c["wxid"] for c in config.get("contacts", [])
                                       if c.get("auto_monitor", True)]
                if not contacts_to_monitor:
                    _print("  ⚠️  没有配置自动监听的联系人", style="yellow")
                    return
                t = _start_monitoring_raw(contacts_to_monitor)
                state.monitor_thread = t
                state.monitor_running = True
            else:
                state.monitor_running = True
            _print("  ▶️  自动监听已开启 (新消息会自动检测并建议回复)", style="green")

    elif cmd == "/copy":
        if last_suggestion:
            try:
                from agent.tools.send import copy_to_clipboard
                copy_to_clipboard(last_suggestion)
                _print(f"  📋 已复制: {last_suggestion[:60]}...", style="green")
            except Exception as e:
                _print(f"  ❌ 复制失败: {e}", style="red")
        else:
            _print("  没有可复制的建议", style="yellow")

    elif cmd == "/send":
        if not last_suggestion:
            _print("  没有可发送的建议", style="yellow")
        else:
            from agent.valve import check_valve, ValveLevel
            try:
                check_valve(ValveLevel.SEND)
            except Exception as e:
                _print(f"  ❌ {e}", style="red")
                return
            try:
                from agent.tools.send import auto_send
                result = auto_send(last_suggestion, contact_name)
                if result["ok"]:
                    _print(f"  ✅ 已发送: {last_suggestion[:60]}", style="green")
                else:
                    _print(f"  ❌ {result['error']}", style="red")
            except Exception as e:
                _print(f"  ❌ 发送失败: {e}", style="red")

    elif cmd == "/voice":
        if len(parts) < 2:
            _print("  用法: /voice auto 或 /voice manual", style="yellow")
        else:
            mode = parts[1]
            if contact_wxid:
                from agent.tools.decode import set_voice_mode
                set_voice_mode(contact_name, mode)
            _print(f"  语音模式已切换为: {mode}", style="green")

    elif cmd == "/contact":
        if len(parts) < 2:
            _print(f"  当前联系人: {contact_name} ({contact_wxid})", style="cyan")
            _print("  切换: /contact <名称>", style="dim")
        else:
            new_contact = " ".join(parts[1:])
            wxid, display = state.resolve_contact_exact(new_contact)
            if wxid:
                return ("switch_contact", display)
            else:
                _print(f"  ❌ 未找到联系人: {new_contact}", style="red")

    elif cmd == "/memory":
        if contact_memory:
            mem_text = contact_memory.memory_text()
            if mem_text:
                _print(f"\n  📝 长期记忆 ({contact_name}):\n", style="cyan")
                _print(mem_text)
            else:
                _print("  尚无记忆，开始对话后会自动积累", style="yellow")
        else:
            _print("  未设置活跃联系人", style="yellow")

    elif cmd == "/save":
        if len(parts) < 2:
            _print("  用法: /save <内容>  — 把重要内容留档（时间点快照，之后可找回来）", style="yellow")
        else:
            from agent.tools.__init__ import _record_note_handler
            content = " ".join(parts[1:])
            result = _record_note_handler(content=content, contact_name=contact_name)
            if result.get("ok"):
                data = result["data"]
                _print(f"  📌 已留档 ({data['contact']})，共 {data['count']} 条", style="green")
            else:
                _print(f"  ❌ {result.get('error')}", style="red")

    elif cmd == "/notes":
        from agent.tools.__init__ import _view_notes_handler
        keyword = " ".join(parts[1:]) if len(parts) > 1 else ""
        result = _view_notes_handler(contact_name=contact_name, keyword=keyword)
        if result.get("ok"):
            data = result["data"]
            notes = data.get("notes", [])
            if not notes:
                _print(f"  没有留档记录（共 {data.get('total', 0)} 条匹配）。用 /save 或让 AI record_note 留档", style="yellow")
            else:
                _print(f"\n  📌 留档 ({data['contact']}) — 显示 {data.get('count', len(notes))} 条，共 {data.get('total', 0)} 条:", style="cyan")
                for n in notes:
                    title = n.get("title") or "(无标题)"
                    _print(f"\n  [{n.get('date', '')}] {title}", style="bold")
                    _print(f"    {n.get('content', '')[:200]}", style="dim")
        else:
            _print(f"  ❌ {result.get('error')}", style="red")

    elif cmd == "/skills":
        active = config.get("active_skills", [])
        _print(f"  活跃技能: {', '.join(active) if active else '无'}", style="cyan")
        _print("  管理技能请使用 list_skills / add_skill 工具 (Phase 4)", style="dim")

    elif cmd == "/clear":
        _print("  对话历史已清除 (系统提示词和记忆保留)", style="green")
        return "clear"

    elif cmd in ("/exit", "/quit"):
        return "exit"

    elif cmd in ("/review", "/r"):
        return "review"

    elif cmd in ("/c", "/coach"):
        return "toggle_coach"

    elif cmd == "/peek":
        if len(parts) < 2:
            _print("  用法: /peek <联系人名称>", style="yellow")
        else:
            target = " ".join(parts[1:])
            from agent.tools.__init__ import _peek_contact_handler
            result = _peek_contact_handler(target)
            if result.get("ok"):
                data = result["data"]
                _print(f"\n  👁️  {target} 的档案:", style="cyan")
                if data.get("memory") and data["memory"] != "(无记忆)":
                    _print(f"\n  [记忆]\n{data['memory'][:600]}", style="dim")
                if data.get("style") and data["style"] != "(无风格分析)":
                    _print(f"\n  [风格]\n{data['style'][:400]}", style="dim")
                if data.get("lessons"):
                    _print(f"\n  [教训] {len(data['lessons'])} 条")
                    for l in data["lessons"][-5:]:
                        _print(f"    - {l.get('title', '?')}", style="dim")
                if data.get("recent_transcript"):
                    _print(f"\n  [最近对话] {len(data['recent_transcript'])} 条", style="dim")
                    for line in data["recent_transcript"][-5:]:
                        _print(f"    {line[:120]}", style="dim")
            else:
                _print(f"  ❌ {result.get('error')}", style="red")

    elif cmd == "/file":
        if len(parts) < 2:
            _print("  用法: /file <文件路径>  — 读取文本文件内容", style="yellow")
        else:
            filepath = " ".join(parts[1:])
            from agent.tools.__init__ import _read_file_handler
            result = _read_file_handler(filepath)
            if result.get("ok"):
                data = result["data"]
                _print(f"\n  📄 {data['filename']} ({data['lines']}行, {data['chars']}字符)", style="cyan")
                _print(f"  {'─' * 60}", style="dim")
                # 如果内容太长，只显示前 2000 字符
                content = data["content"]
                if len(content) > 2000:
                    _print(content[:2000])
                    _print(f"\n  ... (截断，共 {data['chars']} 字符。完整内容通过 AI read_file 工具读取)", style="dim")
                else:
                    _print(content)
            else:
                _print(f"  ❌ {result.get('error')}", style="red")

    else:
        _print(f"  未知命令: {cmd}。输入 /h 查看帮助", style="yellow")


import contextlib  # noqa: E402 — 用于 _console.status nullcontext fallback
