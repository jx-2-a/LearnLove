"""
agent.web — LearnLove 接入 AgentHub 的 Web 入口。

用法:
  python -m agent.web --hub ws://127.0.0.1:8500/ws/agent
  python -m agent.web --hub ws://... --label 恋爱军师 --file-root D:\\... --data-dir D:\\MyData

浏览器是唯一前端：所有 I/O（气泡 / 输入栏 / 工具卡 / 思考块 / 设置面板）走 AgentHub 网页。
终端模式仍用 python -m agent.loop，不受影响。

线程模型（与 Emisinver 参考实现一致）：
  worker 线程   → chat.run_chat(config, session)   # agent 循环（BaseSession 方法线程安全）
  autosave 线程 → 每秒把 sid 落盘 agent_state.json，Hub 重启硬杀也不丢恢复状态
  主线程       → session.run()                      # asyncio 事件循环
"""

import os
import sys
import io
import json
import time
import argparse
import threading
import contextlib
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

from agent import chat
from agent import loop as loop_mod
from agent.paths import get_user_data_dir, set_user_data_dir, config_path, ensure_dirs


# ============================================================================
# 恢复状态（agent_state.json）
# ============================================================================

def _agent_state_file() -> str:
    return os.path.join(get_user_data_dir(), "config", "agent_state.json")


def _load_agent_state() -> dict:
    """读上次会话的恢复状态（sid）；失败/不存在返回空 dict。"""
    p = _agent_state_file()
    try:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_agent_state(data: dict):
    """保存恢复状态（sid）→ 重启自动续接原会话。"""
    try:
        p = _agent_state_file()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================================
# 运行时设置面板（PROTOCOL §6 settings：model/thinking/temperature/max_tokens/valve）
# ============================================================================

_MODEL_OPTIONS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"]


def _build_settings_schema(config: dict) -> list:
    """运行时设置 schema → set_settings 提交给网页设置面板。"""
    llm = config.get("llm", {})
    model = (llm.get("model") or "").strip()
    if model:
        opts = [model] + [m for m in _MODEL_OPTIONS if m != model]
    else:
        opts = list(_MODEL_OPTIONS)
    valve_level = config.get("valve", {}).get("level", 1)
    return [
        {"key": "model", "label": "模型", "type": "select",
         "value": model, "options": [{"label": m, "value": m} for m in opts]},
        {"key": "thinking", "label": "思考模式", "type": "toggle",
         "value": bool(llm.get("thinking", False))},
        {"key": "reasoning_effort", "label": "思考强度", "type": "select",
         "value": llm.get("reasoning_effort", "high"),
         "options": [{"label": "低 low", "value": "low"},
                     {"label": "高 high", "value": "high"},
                     {"label": "最大 max", "value": "max"}]},
        {"key": "temperature", "label": "Temperature", "type": "number",
         "value": llm.get("temperature", 0.7)},
        {"key": "max_tokens", "label": "Max Tokens", "type": "number",
         "value": llm.get("max_tokens", 4096)},
        {"key": "valve", "label": "权限级别", "type": "select",
         "value": str(valve_level),
         "options": [{"label": "L0 只读", "value": "0"},
                     {"label": "L1 建议+剪贴板", "value": "1"},
                     {"label": "L2 自动发送", "value": "2"}]},
    ]


def _make_on_setting(session, schema):
    """网页设置面板改动（settings_set）→ 热重载应用，并重发 settings 让面板刷新显示新值。"""
    def _on_setting(key, value):
        try:
            if key == "valve":
                from agent.valve import ValveLevel, set_valve
                set_valve(ValveLevel(int(value)))
                chat._log(f"权限级别已切换为 L{value}", level="info")
            else:
                chat._RUNTIME[key] = value
                chat._log(f"已应用: {key} = {value}", level="info")
            for s in schema:
                if s.get("key") == key:
                    s["value"] = value
            session.set_settings(schema)
        except Exception:
            pass
    return _on_setting


# ============================================================================
# 主入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="LearnLove Agent — Web 模式（连 AgentHub）")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认: 用户数据目录下的 config.yaml）")
    parser.add_argument("--hub", nargs="?", const=True, default=None,
                        help="连接 AgentHub。传 ws://host:port/ws/agent，或 --hub 用环境变量 AGENT_HUB")
    parser.add_argument("--label", default="", help="Hub 会话显示名（默认 AGENT_LABEL 或 '恋爱军师'）")
    parser.add_argument("--file-root", action="append", default=[],
                        help="Hub 文件服务根目录（可多次），默认 AGENT_FILE_ROOTS 分号分隔")
    parser.add_argument("--data-dir", default=None, help="用户数据目录")
    args = parser.parse_args()

    hub = args.hub if args.hub is not True else os.environ.get("AGENT_HUB", "").strip()
    if not hub:
        parser.error("请用 --hub ws://host:port/ws/agent 连接 AgentHub（或设置 AGENT_HUB 环境变量）")
    if not hub.startswith("ws://") and not hub.startswith("wss://"):
        hub = "ws://" + hub

    # ---- 数据目录 ----
    if args.data_dir:
        set_user_data_dir(args.data_dir)
    else:
        get_user_data_dir()   # 触发默认值解析（环境变量或 ~/.learnlove_data）
    ensure_dirs()

    # ---- 配置 ----
    cfg_path = args.config or config_path()
    if not os.path.exists(cfg_path):
        print(f"[!] 配置文件不存在: {cfg_path}")
        sys.exit(1)
    config = loop_mod.load_config(cfg_path)

    # ---- 初始化（捕获启动期输出 → welcome 气泡上屏） ----
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        loop_mod.initialize(config)
    startup_text = buf.getvalue()

    # ---- 恢复状态：重启续接上次会话（resume_sid → Hub 复用旧 sid，完整历史回放） ----
    saved = _load_agent_state()
    if os.environ.get("AGENT_HUB_FRESH"):
        saved = {}   # 新实例：忽略旧状态，开新会话
    resume_sid = (saved.get("sid") or "").strip() or None

    file_roots = list(args.file_root) or [
        p.strip() for p in os.environ.get("AGENT_FILE_ROOTS", "").split(";") if p.strip()]
    # 文件服务根：项目根 + 用户数据目录（记忆/复盘/留档都在其中）+ 外部传入
    file_roots = list(dict.fromkeys([PROJECT_ROOT, get_user_data_dir()] + file_roots))

    label = args.label or os.environ.get("AGENT_LABEL", "").strip() or "恋爱军师"

    from agentweb.client import WebSessionClient
    session = WebSessionClient(
        hub_url=hub,
        label=label,
        file_roots=file_roots,
        resume_sid=resume_sid,
    )

    # 提前注入全局 _session，让启动气泡走网页（run_chat 会再设一次，幂等）
    chat._session = session

    # 运行时设置面板
    schema = _build_settings_schema(config)
    session.on_setting = _make_on_setting(session, schema)
    session.set_settings(schema)

    # 启动行 → silent（只落转录不上屏）；初始化摘要 → 普通 info 气泡（非欢迎横幅，
    # LearnLove 的欢迎由 run_chat 的终端横幅承担）
    chat._log(f"[{datetime.now().strftime('%H:%M:%S')}] LearnLove Agent 启动", level="silent")
    if startup_text.strip():
        chat._log(startup_text.strip("\n"), level="info")

    # 上报会话分类信息 → Hub 更新会话/实例标签
    session.set_meta(label=label, project_root=get_user_data_dir())

    # 配置启用了自动监听 → 启动后台监控（新消息会自动检测并建议）
    if config.get("agent", {}).get("auto", {}).get("enabled", False):
        contacts_to_monitor = [c["wxid"] for c in config.get("contacts", [])
                               if c.get("auto_monitor", True)]
        if contacts_to_monitor:
            from agent.tools.monitor import _start_monitoring_raw
            from agent.tools._state import state as st
            t = _start_monitoring_raw(contacts_to_monitor)
            st.monitor_thread = t
            st.monitor_running = True
            chat._log(f"▶ 自动监听已开启: {', '.join(contacts_to_monitor)}", level="info")

    def _worker():
        try:
            chat.run_chat(config, session)
        except SystemExit:
            pass   # 配置/初始化阶段已处理的退出，静默
        except Exception:
            import traceback
            chat._log(f"⚠ Agent 异常退出:\n{traceback.format_exc()}", level="important")
        finally:
            session.stop()

    def _autosave():
        """注册成功即落盘 sid（Hub 重启是硬杀，只在退出时存会丢）。"""
        last = None
        while True:
            sid = getattr(session, "_sid", None)
            if sid and sid != last:
                _save_agent_state({"sid": sid})
                last = sid
            time.sleep(1)

    threading.Thread(target=_autosave, daemon=True).start()
    threading.Thread(target=_worker, daemon=True).start()
    try:
        session.run()
    finally:
        chat._session = None
        session.close()
        if session._sid:
            _save_agent_state({"sid": session._sid})


if __name__ == "__main__":
    main()
