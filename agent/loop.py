"""
入口 — LearnLove Agent 启动器

用法:
  python -m agent.loop                          # 交互模式
  python -m agent.loop --auto                    # 自动监听模式
  python -m agent.loop --data-dir D:\\MyData      # 指定数据目录
  python -m agent.loop --daemon                  # 守护模式

用户数据目录优先级:
  1. 命令行 --data-dir
  2. 环境变量 LEARNLOVE_USER_DATA
  3. 默认 ~/.learnlove_data
"""

import os
import sys
import json
import argparse
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)


def _migrate_old_data():
    """检查旧数据目录是否存在，迁移到新的 USER_DATA_DIR。"""
    from agent.paths import get_user_data_dir, ensure_dirs

    old_data = os.path.join(PROJECT_ROOT, "data", "agent")
    old_live = os.path.join(PROJECT_ROOT, "data", "live")
    old_voice = os.path.join(PROJECT_ROOT, "data", "voice_cache")
    old_config = os.path.join(SCRIPT_DIR, "config.yaml")
    new_root = get_user_data_dir()

    # 检查是否需要迁移
    has_old = os.path.exists(old_data) or os.path.exists(old_config)
    if not has_old:
        ensure_dirs()
        return

    # 检查新目录是否已有数据
    new_memory = os.path.join(new_root, "memory")
    new_config = os.path.join(new_root, "config.yaml")
    if os.path.exists(new_config) or (os.path.exists(new_memory) and os.listdir(new_memory)):
        return  # 新目录已有数据，跳过

    print("\n" + "=" * 50)
    print("  📦 检测到旧数据，正在迁移到用户数据目录...")
    print(f"  旧位置: {PROJECT_ROOT}\\data\\")
    print(f"  新位置: {new_root}")
    print("=" * 50)

    ensure_dirs()

    migrated = 0

    # memory/
    if os.path.exists(old_data):
        for item in os.listdir(os.path.join(old_data, "memory")) if os.path.exists(os.path.join(old_data, "memory")) else []:
            src = os.path.join(old_data, "memory", item)
            dst = os.path.join(new_root, "memory", item)
            if os.path.isdir(src) and not os.path.exists(dst):
                shutil.copytree(src, dst)
                print(f"  ✓ memory/{item}/")
                migrated += 1

    # styles/
    old_styles = os.path.join(old_data, "styles")
    if os.path.exists(old_styles):
        new_styles = os.path.join(new_root, "styles")
        os.makedirs(new_styles, exist_ok=True)
        for f in os.listdir(old_styles):
            src = os.path.join(old_styles, f)
            dst = os.path.join(new_styles, f)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                print(f"  ✓ styles/{f}")
                migrated += 1

    # spills/
    old_spills = os.path.join(old_data, "spills")
    if os.path.exists(old_spills):
        new_spills = os.path.join(new_root, "spills")
        os.makedirs(new_spills, exist_ok=True)
        for f in os.listdir(old_spills):
            src = os.path.join(old_spills, f)
            dst = os.path.join(new_spills, f)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        if os.listdir(old_spills):
            print(f"  ✓ spills/ ({len(os.listdir(old_spills))} files)")

    # lessons.json → 迁移到第一个联系人的目录
    old_lessons = os.path.join(old_data, "lessons.json")
    if os.path.exists(old_lessons):
        # 尝试找第一个有记忆目录的联系人
        mem_root = os.path.join(new_root, "memory")
        contacts = [d for d in os.listdir(mem_root) if os.path.isdir(os.path.join(mem_root, d))]
        if contacts:
            dst = os.path.join(mem_root, contacts[0], "lessons.json")
            shutil.copy2(old_lessons, dst)
            print(f"  ✓ lessons.json → memory/{contacts[0]}/lessons.json")

    # live/
    if os.path.exists(old_live):
        new_live = os.path.join(new_root, "live")
        os.makedirs(new_live, exist_ok=True)
        for f in os.listdir(old_live):
            src = os.path.join(old_live, f)
            dst = os.path.join(new_live, f)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
        if os.listdir(old_live):
            print(f"  ✓ live/ ({len(os.listdir(old_live))} files)")

    # voice_cache/
    if os.path.exists(old_voice):
        new_voice = os.path.join(new_root, "voice_cache")
        os.makedirs(new_voice, exist_ok=True)
        for f in os.listdir(old_voice):
            src = os.path.join(old_voice, f)
            dst = os.path.join(new_voice, f)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
        if os.listdir(old_voice):
            print(f"  ✓ voice_cache/ ({len(os.listdir(old_voice))} files)")

    # config.yaml
    if os.path.exists(old_config):
        new_config_path = os.path.join(new_root, "config.yaml")
        if not os.path.exists(new_config_path):
            shutil.copy2(old_config, new_config_path)
            print(f"  ✓ config.yaml")
            print(f"\n  ⚠️  旧 config.yaml 仍在项目目录中!")
            print(f"     请手动删除: {old_config}")
            print(f"     它包含你的 API key，不应提交到 git。")

    if migrated > 0:
        print(f"\n  ✅ 迁移完成 ({migrated} 项)。旧数据保留在原位置，确认后可手动删除。")
    print("")


def load_config(config_path: str) -> dict:
    """加载 config.yaml，解析相对路径"""
    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 解析路径：展开 ~ 和相对路径
    if "wechat" in config:
        wechat = config["wechat"]
        for key in ("keys_file", "decrypted_dir"):
            if key in wechat:
                wechat[key] = os.path.expanduser(wechat[key])
                if not os.path.isabs(wechat[key]):
                    wechat[key] = os.path.join(PROJECT_ROOT, wechat[key])

    return config


def _ensure_wechat_ready(db_dir: str, keys_file: str):
    """自动检测并按需初始化微信密钥和解密数据库。

    只在密钥缺失或失效时才触发。
    """
    # 快速路径：密钥文件存在
    if os.path.exists(keys_file):
        try:
            with open(keys_file) as f:
                if json.load(f):
                    return  # 有内容，假定有效
        except Exception:
            pass

    print("\n" + "=" * 50)
    print("  [Agent] 检测到微信密钥缺失，自动初始化...")
    print("=" * 50)

    # 调用 wechat-decrypt/setup.py 的检测+初始化逻辑
    setup_py = os.path.join(PROJECT_ROOT, "wechat-decrypt", "setup.py")
    if os.path.exists(setup_py):
        import subprocess as _sp
        r = _sp.run(
            [sys.executable, setup_py, "--json"],
            capture_output=True, text=True, timeout=600,
            cwd=os.path.dirname(setup_py)
        )
        if r.returncode == 0:
            try:
                result = json.loads(r.stdout.split("\n")[-1])
                if result.get("success"):
                    print("  [Agent] 初始化成功！\n")
                    return
            except json.JSONDecodeError:
                pass
        print(f"  [!] 自动初始化失败，请手动运行:")
        print(f"      python {setup_py}")
    else:
        print(f"  [!] 未找到 setup.py，请手动提取密钥:")
        print(f"      1. 打开微信并登录")
        print(f"      2. python wechat-decrypt/find_all_keys.py")
        print(f"      3. python wechat-decrypt/decrypt_db.py")

    print()


def initialize(config: dict):
    """初始化所有子系统

    1. 自动检测并按需提取密钥、解密数据库
    2. 加载微信密钥
    3. 创建 DBCache
    4. 加载联系人
    5. 设置全局状态
    """
    from agent.tools._state import state

    wechat_cfg = config.get("wechat", {})
    db_dir = wechat_cfg.get("db_dir", "")
    keys_file = wechat_cfg.get("keys_file", "")

    # 自动初始化（密钥缺失/失效时才会触发）
    if not os.path.exists(keys_file):
        _ensure_wechat_ready(db_dir, keys_file)

    if not os.path.exists(keys_file):
        print(f"[!] 密钥文件不存在: {keys_file}")
        print("    请手动运行: python wechat-decrypt/setup.py")
        sys.exit(1)

    print(f"[*] 初始化微信数据...")
    state.setup(db_dir, keys_file)
    state.load_contacts()
    state.config = config

    print(f"    DBCache 就绪 ({len(state.db_cache.all_keys)} 个加密数据库)")
    print(f"    已加载 {len(state.contacts)} 个联系人")

    # 初始化 SkillManager
    from agent.tool_manager import skillmgr
    skills_path = os.path.join(SCRIPT_DIR, "skills.yaml")
    active = config.get("active_skills", [])
    skillmgr.load(skills_path, active)
    print(f"    SkillManager: {len(skillmgr._skills)} 个技能已加载, 活跃: {active}")

    # 验证配置的联系人存在
    contacts_cfg = config.get("contacts", [])
    for c in contacts_cfg:
        wxid = c.get("wxid", "")
        name = c.get("name", "")
        if wxid not in state.contacts:
            found_wxid, found_name = state.resolve_contact(name)
            if found_wxid:
                print(f"    {name} → wxid: {found_wxid}")
                c["wxid"] = found_wxid
            else:
                print(f"    [!] 未在联系人列表中找到: {name} ({wxid})")
        else:
            print(f"    ✓ {name}")

    return state


def main():
    parser = argparse.ArgumentParser(description="LearnLove Agent — 微信聊天助手")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认: 用户数据目录下的 config.yaml）")
    parser.add_argument("--data-dir", default=None,
                        help="用户数据目录（默认: ~/.learnlove_data 或 $LEARNLOVE_USER_DATA）")
    parser.add_argument("--auto", action="store_true",
                        help="启动时进入自动监听模式")
    parser.add_argument("--daemon", action="store_true",
                        help="守护模式：仅后台监控写入 incoming.jsonl，不启动 REPL")
    args = parser.parse_args()

    # ---- 数据目录设置 ----
    from agent.paths import set_user_data_dir, get_user_data_dir, ensure_dirs
    if args.data_dir:
        set_user_data_dir(args.data_dir)
    else:
        # 触发默认值解析（环境变量或 ~/.learnlove_data）
        get_user_data_dir()

    # ---- 迁移旧数据 ----
    _migrate_old_data()

    # ---- 确定配置文件路径 ----
    from agent.paths import config_path
    config_path = args.config or config_path()
    if not os.path.exists(config_path):
        # 检查旧位置
        old_config = os.path.join(SCRIPT_DIR, "config.yaml")
        if os.path.exists(old_config):
            print(f"[!] 请将配置文件移到数据目录: {config_path}")
            print(f"    当前仍位于项目内: {old_config}")
            print(f"    或使用 --config {old_config} 临时加载")
            sys.exit(1)
        print(f"[!] 配置文件不存在: {config_path}")
        print(f"    请从 agent/config.sample.yaml 复制并填写真实配置")
        print(f"    数据目录: {get_user_data_dir()}")
        sys.exit(1)

    # ---- 安全检查 ----
    old_config = os.path.join(SCRIPT_DIR, "config.yaml")
    if os.path.exists(old_config) and os.path.exists(config_path):
        print("⚠️  警告: 项目目录中仍存在 config.yaml（含 API key）!")
        print(f"   请手动删除: {old_config}")
        print("")

    print("=" * 50)
    print("  LearnLove Agent — 微信聊天助手")
    print(f"  数据目录: {get_user_data_dir()}")
    print("=" * 50)

    config = load_config(config_path)
    state = initialize(config)

    # 处理 auto 标志
    if args.auto:
        config["agent"]["auto"]["enabled"] = True

    # 守护模式
    if args.daemon:
        print("[*] 守护模式：启动后台监控...")
        from agent.tools._state import state as st
        monitor_contacts = [
            c["wxid"] for c in config.get("contacts", [])
            if c.get("auto_monitor", True)
        ]
        if not monitor_contacts:
            print("[!] 没有配置自动监听的联系人")
            sys.exit(1)

        from agent.tools.monitor import _start_monitoring_raw
        t = _start_monitoring_raw(monitor_contacts)
        print(f"[*] 监听已启动: {', '.join(monitor_contacts)}")
        print(f"[*] 消息写入: {st.live_feed_path}")
        print(f"[*] Ctrl+C 停止")

        try:
            while t.is_alive():
                t.join(1)
        except KeyboardInterrupt:
            print("\n[*] 停止监听...")
            st.monitor_running = False
        return

    # REPL 模式
    from agent.chat import run_chat

    # 如果配置启用了 auto
    if config.get("agent", {}).get("auto", {}).get("enabled", False):
        contacts_to_monitor = [
            c["wxid"] for c in config.get("contacts", [])
            if c.get("auto_monitor", True)
        ]
        if contacts_to_monitor:
            from agent.tools.monitor import _start_monitoring_raw
            from agent.tools._state import state as st
            t = _start_monitoring_raw(contacts_to_monitor)
            st.monitor_thread = t
            st.monitor_running = True
            print("[*] 自动监听模式已启动")

    run_chat(config)


if __name__ == "__main__":
    main()
