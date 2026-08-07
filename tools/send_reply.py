"""
发送微信回复 — 两种模式

安全模式 (推荐):
    python tools/send_reply.py --text "好的，晚安~" --copy
    → 文字复制到剪贴板 → 你切到微信 Ctrl+V 回车

自动模式 (微信窗口需打开):
    python tools/send_reply.py --text "好的，晚安~" --send --to "联系人名称"
    → 自动激活微信 → 搜索联系人 → 粘贴发送

依赖:
    pip install pyautogui pygetwindow pyperclip pywinauto
"""
import os, sys, json, argparse, subprocess, time

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============ 剪贴板 ============

def copy_to_clipboard(text):
    """复制到 Windows 剪贴板"""
    process = subprocess.Popen(['clip'], stdin=subprocess.PIPE, shell=True)
    process.communicate(input=text.encode('utf-16-le', errors='replace'))
    process.wait()
    # 验证
    result = subprocess.run(
        ['powershell', '-Command', 'Get-Clipboard'],
        capture_output=True, text=True
    )
    return result.stdout.strip()


# ============ 自动发送 ============

def find_wx_window():
    """找到微信主窗口"""
    try:
        import pygetwindow as gw
        windows = [w for w in gw.getAllWindows()
                   if w.title and '微信' in w.title and w.width > 100]
        if windows:
            return windows[0]
    except:
        pass
    return None


def auto_send(text, contact_name=None):
    """自动发送微信消息"""
    try:
        import pyautogui
        import pyperclip
    except ImportError as e:
        return False, f"缺少依赖: {e}\n安装: pip install pyautogui pyperclip pygetwindow"

    wx = find_wx_window()
    if not wx:
        return False, "未找到微信窗口，请打开微信"

    # 保存旧剪贴板
    try:
        result = subprocess.run(
            ['powershell', '-Command', 'Get-Clipboard'],
            capture_output=True, text=True
        )
        old_clip = result.stdout
    except:
        old_clip = ""

    try:
        # 写入剪贴板
        pyperclip.copy(text)

        # 恢复/激活微信
        if wx.isMinimized:
            wx.restore()
            time.sleep(0.3)
        wx.activate()
        time.sleep(0.3)

        # 如果指定联系人，先搜索
        if contact_name:
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(0.3)
            # 清除搜索框
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.05)
            pyautogui.press('backspace')
            time.sleep(0.1)
            # 输入联系人名
            pyperclip.copy(contact_name)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.5)
            # 选中第一个搜索结果
            pyautogui.press('enter')
            time.sleep(0.3)

        # 粘贴消息
        # 确保焦点在输入框
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.2)

        # 发送
        pyautogui.press('enter')

        # 恢复旧剪贴板
        if old_clip:
            pyperclip.copy(old_clip)

        return True, "发送成功"

    except Exception as e:
        return False, f"发送失败: {e}"


# ============ CLI ============

def main():
    parser = argparse.ArgumentParser(description="发送微信回复")
    parser.add_argument("--text", required=True, help="要发送的文字")
    parser.add_argument("--copy", action="store_true", help="复制到剪贴板")
    parser.add_argument("--send", action="store_true", help="自动发送到微信")
    parser.add_argument("--to", help="发送给谁（自动模式需要）")
    parser.add_argument("--check", action="store_true", help="检查微信窗口状态")
    args = parser.parse_args()

    if args.check:
        wx = find_wx_window()
        if wx:
            print(f"[+] 微信窗口: {wx.title}")
            print(f"    可见={wx.visible} 最小化={wx.isMinimized}")
            print(f"    大小={wx.width}x{wx.height}")
        else:
            print("[-] 未找到微信窗口")
        return

    if not args.text.strip():
        print("[!] 消息为空")
        return

    print(f"[+] 消息: {args.text[:100]}{'...' if len(args.text) > 100 else ''}")

    if args.send:
        print("[*] 自动发送中...")
        ok, msg = auto_send(args.text, args.to)
        if ok:
            print(f"[OK] {msg}")
            return
        else:
            print(f"[!] {msg}")
            print("[!] 回退到剪贴板模式")

    # 默认：剪贴板模式
    print("[*] 复制到剪贴板...")
    clipped = copy_to_clipboard(args.text)
    print(f"[OK] 剪贴板: {clipped[:60]}...")
    print()
    print("  >>> 切到微信 → 点输入框 → Ctrl+V → Enter 发送 <<<")


if __name__ == "__main__":
    main()
