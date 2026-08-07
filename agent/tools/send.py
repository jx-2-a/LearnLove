"""
消息发送 — 剪贴板 + pyautogui 自动发送

模式:
  copy_to_clipboard: 复制到 Windows 剪贴板 (阀门 L1)
  auto_send: 自动激活微信窗口 → 搜索联系人 → 粘贴发送 (阀门 L2)
"""

import subprocess
import time
from agent.protocol import ok, err
from agent.valve import check_valve, ValveLevel
from agent.tools._state import state


# ===== 剪贴板 =====

def copy_to_clipboard(text: str) -> dict:
    """复制文本到 Windows 剪贴板 (UTF-16-LE)

    需要阀门 L1+
    """
    check_valve(ValveLevel.SUGGEST)
    try:
        process = subprocess.Popen(['clip'], stdin=subprocess.PIPE, shell=True)
        process.communicate(input=text.encode('utf-16-le', errors='replace'))
        process.wait()
        return ok({"clipped": text[:200]})
    except Exception as e:
        return err(f"剪贴板写入失败: {e}")


# ===== 微信窗口检测 =====

def _find_wx_window():
    """查找微信主窗口"""
    try:
        import pygetwindow as gw
        windows = [w for w in gw.getAllWindows()
                   if w.title and '微信' in w.title and w.width > 100]
        if windows:
            return windows[0]
    except Exception:
        pass
    return None


def check_wechat_window() -> dict:
    """检查微信窗口状态"""
    wx = _find_wx_window()
    if wx:
        return ok({
            "available": True,
            "title": wx.title,
            "visible": wx.visible,
            "minimized": wx.isMinimized,
            "size": f"{wx.width}x{wx.height}",
        })
    return ok({"available": False, "note": "未找到微信窗口，请确认微信已打开且窗口标题包含'微信'"})


# ===== 自动发送 =====

def auto_send(text: str, contact_name: str) -> dict:
    """自动发送微信消息 (pyautogui)

    需要阀门 L2。
    流程：保存旧剪贴板 → 激活微信 → Ctrl+F 搜索 → 粘贴联系人 → Enter →
          Ctrl+V 粘贴消息 → Enter 发送 → 恢复旧剪贴板
    """
    check_valve(ValveLevel.SEND)

    try:
        import pyautogui
        import pyperclip
    except ImportError as e:
        return err(f"缺少依赖: {e}。安装: pip install pyautogui pyperclip pygetwindow")

    wx = _find_wx_window()
    if not wx:
        return err("未找到微信窗口，请打开微信")

    # 保存旧剪贴板
    try:
        result = subprocess.run(
            ['powershell', '-Command', 'Get-Clipboard'],
            capture_output=True, text=True, timeout=5,
        )
        old_clip = result.stdout
    except Exception:
        old_clip = ""

    try:
        # 写入新剪贴板
        pyperclip.copy(text)

        # 恢复/激活微信窗口
        if wx.isMinimized:
            wx.restore()
            time.sleep(0.3)
        wx.activate()
        time.sleep(0.3)

        # 搜索联系人
        if contact_name:
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(0.3)
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.05)
            pyautogui.press('backspace')
            time.sleep(0.1)
            pyperclip.copy(contact_name)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(0.3)

        # 粘贴消息
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.2)

        # 发送
        pyautogui.press('enter')

        # 恢复旧剪贴板
        if old_clip:
            try:
                pyperclip.copy(old_clip)
            except Exception:
                pass

        return ok({"sent": True, "text": text[:200], "contact": contact_name})

    except Exception as e:
        # 出错也尝试恢复剪贴板
        if old_clip:
            try:
                import pyperclip
                pyperclip.copy(old_clip)
            except Exception:
                pass
        return err(f"发送失败: {e}")
