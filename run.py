"""
LearnLove — 微信聊天记录分析工具集

用法:
    # 1. 提取聊天记录
    python tools/extract_chat.py --name "谢雨欣" --days 90

    # 2. 批量语音转文字
    python tools/transcribe_voice.py --name "谢雨欣" --model small

    # 3. 实时监听新消息（后台运行）
    python tools/live_monitor.py --name "谢雨欣"

    # 4. 列出所有会话
    python tools/extract_chat.py --list

    # 5. 列出有语音的会话
    python tools/transcribe_voice.py --list-chats

流程:
    解密DB → 提取文字 → 转录语音 → 分析
    decrypt_db.py → extract_chat.py → transcribe_voice.py → Claude分析

实时回复建议流程:
    live_monitor.py (后台) → MCP get_live_feed → Claude 分析 → 回复建议
"""
import os, sys, argparse, subprocess

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)

VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")


def run_script(name, *args):
    """运行 tools/ 下的脚本"""
    script = os.path.join(PROJECT_ROOT, "tools", name)
    cmd = [VENV_PYTHON, script] + list(args)
    subprocess.run(cmd)


def main():
    parser = argparse.ArgumentParser(
        description="LearnLove 微信聊天分析工具",
        epilog="示例: python run.py transcribe --name 谢雨欣"
    )
    sub = parser.add_subparsers(dest="command")

    # extract
    p = sub.add_parser("extract", help="提取聊天记录")
    p.add_argument("--name", required=True)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--output", default="data/chats")

    # transcribe
    p = sub.add_parser("transcribe", help="批量语音转文字")
    p.add_argument("--name", required=True)
    p.add_argument("--model", default="small", choices=["tiny", "small", "medium"])
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")

    # live
    p = sub.add_parser("live", help="实时监听新消息")
    p.add_argument("--name", required=True)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--model", default="small")

    # list sessions
    sub.add_parser("list", help="列出所有会话")

    # list voice chats
    sub.add_parser("voices", help="列出有语音的会话")

    args = parser.parse_args()

    if args.command == "extract":
        run_script("extract_chat.py",
                   "--name", args.name,
                   "--days", str(args.days),
                   "--output", args.output)
    elif args.command == "transcribe":
        cmd_args = ["transcribe_voice.py", "--name", args.name, "--model", args.model]
        if args.limit:
            cmd_args += ["--limit", str(args.limit)]
        if args.dry_run:
            cmd_args.append("--dry-run")
        run_script(*cmd_args)
    elif args.command == "live":
        run_script("live_monitor.py",
                   "--name", args.name,
                   "--interval", str(args.interval),
                   "--model", args.model)
    elif args.command == "list":
        run_script("extract_chat.py", "--list")
    elif args.command == "voices":
        run_script("transcribe_voice.py", "--list-chats")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
