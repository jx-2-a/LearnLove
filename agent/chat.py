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
from agent.llm import chat as llm_chat, _get_tools
from agent.context import ContextBuilder
from agent.memory import ContactMemory
from agent.tools import dispatch as tool_dispatch
from agent.tools._state import state
from agent.paths import context_json_path


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


def _print(text: str, style: str = ""):
    """安全打印"""
    if _console:
        if style:
            _console.print(text, style=style)
        else:
            _console.print(text)
    else:
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
    if _console:
        from rich.markdown import Markdown
        _console.print(Markdown(help_text))
    else:
        print(help_text)


# ===== 工具调用循环 =====

def _execute_tool_loop(messages: list[dict], llm_cfg: dict,
                       active_skills: list[str] = None) -> str:
    """执行工具调用循环：LLM 返回 tool_calls → 执行 → 继续，直到返回文本。
    无轮数上限，LLM 可以持续调用工具直到产出文本回复。

    Returns:
        LLM 最终文本回复，或错误信息
    """
    tools = _get_tools()
    round_num = 0

    while True:
        round_num += 1
        result = llm_chat(
            messages=messages,
            tools=tools,
            api_base=llm_cfg.get("api_base", ""),
            api_key=llm_cfg.get("api_key", ""),
            model=llm_cfg.get("model", ""),
            temperature=llm_cfg.get("temperature", 0.7),
            max_tokens=llm_cfg.get("max_tokens", 4096),
        )

        if result["type"] == "text":
            return result["content"]

        elif result["type"] == "error":
            return f"[LLM 错误] {result['content']}"

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
            messages.append(assistant_msg)

            # 串行执行每个工具调用
            for call in result["calls"]:
                tool_name = call["name"]
                tool_args = call["args"]
                call_id = call["id"]

                _print(f"  🔧 {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:80]})", style="dim")

                # 执行
                try:
                    tool_result = tool_dispatch(tool_name, **tool_args)
                except Exception as e:
                    tool_result = err(f"工具执行异常: {e}")

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

def run_chat(config: dict):
    """主 REPL 入口

    1. 初始化状态（加载联系人、设置活跃联系人）
    2. 加载记忆
    3. 显示欢迎信息
    4. 进入交互循环
    """
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

            # 处理自动模式的新消息
            if auto_new:
                for entry in auto_new:
                    entry_contact = entry.get("sender", "?")
                    entry_content = entry.get("content", "")
                    entry_type = entry.get("type", 1)

                    _print(f"\n  📩 [自动] {entry_contact} ({entry.get('time', '')}): {entry_content[:100]}", style="yellow")

                    # 检查是否是语音消息，根据模式处理
                    if entry_type == 34:  # 语音
                        entry_wxid = entry.get("wxid", "")
                        voice_cfg = state.voice_mode(entry_wxid)
                        if voice_cfg == "manual":
                            _print("  🎤 语音消息 — 手动模式，请描述语音内容或输入 /voice auto 切换", style="yellow")
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

                    # 确定消息归属的联系人（匹配名称和记忆）
                    msg_wxid = entry.get("wxid", "")
                    msg_contact_name = state.contacts.get(msg_wxid, {}).get("display", entry_contact)

                    # 如果当前活跃联系人不是消息发送者，自动切换
                    if msg_contact_name and msg_contact_name != contact_name:
                        _print(f"  🔄 自动切换联系人: {contact_name} → {msg_contact_name}", style="dim")
                        contact_name = msg_contact_name
                        contact_wxid = msg_wxid
                        contact_memory = ContactMemory(contact_name, llm_cfg)
                        contact_memory.startup()

                    _print("  ▸ 生成回复建议...", style="dim")

                    # 构建上下文
                    msg_context = ctx_builder.build_context(
                        contact_name=contact_name,
                        contact_wxid=contact_wxid,
                        new_message={
                            "sender": entry_contact,
                            "content": entry_content,
                            "time": entry.get("time", ""),
                        },
                        memory_text=contact_memory.memory_text() if contact_memory else "",
                        skill_modifiers=_get_skill_modifiers(),
                        max_tokens=agent_cfg.get("context", {}).get("max_context_tokens", 8000),
                    )

                    # LLM 工具调用循环
                    with _console.status("[cyan]思考中...[/cyan]") if _console else contextlib.nullcontext():
                        reply = _execute_tool_loop(msg_context, llm_cfg, active_skills)

                    if reply and not reply.startswith("[系统]"):
                        _print(f"\n  🤖 建议回复:", style="bold green")
                        _print(f"  {reply}", style="green")
                        last_suggestion = reply

                        # 自动复制到剪贴板
                        if agent_cfg.get("auto", {}).get("auto_copy", False):
                            try:
                                from agent.tools.send import copy_to_clipboard
                                copy_to_clipboard(reply)
                                _print("  📋 已复制到剪贴板", style="dim")
                            except Exception:
                                pass

                        # 记录记忆
                        if contact_memory:
                            contact_memory.log_turn(
                                incoming_msg=entry_content,
                                incoming_sender=entry_contact,
                                suggested_reply=reply,
                            )

                            # 检查是否需要压缩
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
            if state.monitor_running:
                # 自动模式：先显示提示符，再轮询等待（不阻塞监控队列）
                import msvcrt
                user_input = None
                p = "咨询 > " if coach_mode else "你 > "
                for i in range(40):  # 最多等 20 秒（40 × 0.5s）
                    if i == 0:
                        # 在轮询开始前就显示提示符，用户知道可以输入
                        sys.stdout.write(p)
                        sys.stdout.flush()
                    if msvcrt.kbhit():
                        # 提示符已经显示，用空提示符读取（避免重复打印）
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
                else:
                    # 超时无输入无新消息，换行后继续循环
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    continue
                # 因为新消息退出 → 回到循环顶部 drain+处理
                if user_input is None:
                    continue
            else:
                try:
                    prompt = "咨询 > " if coach_mode else "你 > "
                    user_input = input(prompt).strip()
                except (EOFError, KeyboardInterrupt):
                    break

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
                    _do_review(contact_name, contact_wxid, llm_cfg, config)
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

            # LLM 工具调用循环
            with _console.status("[cyan]思考中...[/cyan]") if _console else contextlib.nullcontext():
                reply = _execute_tool_loop(msg_context, llm_cfg, active_skills)

            if reply:
                if reply.startswith("[LLM 错误]") or reply.startswith("[系统]"):
                    _print(f"  ❌ {reply}", style="red")
                else:
                    _print(f"\n  🤖 {reply}\n", style="green")
                    last_suggestion = reply

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


def _do_review(contact_name: str, contact_wxid: str, llm_cfg: dict, config: dict):
    """复盘分析：基于时间戳增量拉取聊天记录，用教练视角分析对话，帮用户学习成长。"""
    from agent.tools.message import get_chat_history
    from agent.llm import chat_simple
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

    if not result.get("ok"):
        _print(f"  ❌ 获取聊天记录失败: {result.get('error', '未知错误')}", style="red")
        return

    messages = result["data"].get("messages", [])
    if not messages:
        if total_reviews > 0:
            _print(f"  📭 {contact_name} 暂无新消息（上次复盘后没有新的聊天记录）", style="yellow")
        else:
            _print(f"  📭 暂无 {contact_name} 的聊天记录", style="yellow")
        return

    # ---- 4. 正序排列（按时间顺序，更符合复盘阅读习惯） ----
    messages_asc = sorted(messages, key=lambda m: m["create_time"])
    min_ts = messages_asc[0]["create_time"]
    max_ts = messages_asc[-1]["create_time"]

    # ---- 5. 检测是否达到拉取上限 ----
    hit_limit = len(messages) >= min(initial_limit, 200)

    # ---- 6. 格式化聊天记录 ----
    lines = []
    for m in messages_asc:
        sender = m.get("sender", "?")
        content = m.get("content", "")
        t = m.get("time", "")
        msg_type = m.get("type", "")
        if len(content) > 500:
            content = content[:500] + "..."
        type_tag = f" [{msg_type}]" if msg_type and msg_type != "文本" else ""
        lines.append(f"[{t}] {sender}{type_tag}: {content}")

    chat_text = "\n".join(lines)
    time_range = (
        f"{datetime.fromtimestamp(min_ts).strftime('%m-%d %H:%M')} ~ "
        f"{datetime.fromtimestamp(max_ts).strftime('%m-%d %H:%M')}"
    )
    _print(f"  📊 已加载 {len(messages)} 条消息（{time_range}），正在分析...", style="cyan")

    # ---- 7. 构建 user_prompt ----
    user_prompt = (
        f"## 联系人: {contact_name}\n"
        f"## 时间范围: {time_range}\n"
        f"## 聊天记录（共 {len(messages)} 条）\n"
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
            f"{prev_summary[:500]}\n"
            f"(以上为上次复盘的关键结论，请结合上下文注意前后变化和趋势)"
        )

    full_system = REVIEW_SYSTEM_PROMPT + previous_context + skill_context

    # ---- 10. 调用 LLM 分析 ----
    _print("  ▸ 分析中...", style="dim")
    try:
        analysis = chat_simple(
            user_message=user_prompt,
            system_prompt=full_system,
            config=llm_cfg,
        )
    except Exception as e:
        _print(f"  ❌ 分析失败: {e}", style="red")
        return

    if not analysis or analysis.startswith("[LLM 错误]"):
        _print(f"  ❌ 分析失败: {analysis}", style="red")
        return

    # ---- 11. 输出分析结果 ----
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
        "last_summary": analysis[:1000],
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
