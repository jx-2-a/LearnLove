"""
权限门控 — 在工具边界强制检查权限级别

L0 READ    — 只读（查看消息、联系人、历史）
L1 SUGGEST — 生成回复建议、复制到剪贴板
L2 SEND    — 自动发送 (pyautogui)
"""

from enum import IntEnum
from typing import Dict


class ValveLevel(IntEnum):
    READ = 0
    SUGGEST = 1
    SEND = 2


class PermissionError(Exception):
    """权限不足"""
    pass


_current_valve: Dict[str, object] = {"level": ValveLevel.READ}


def set_valve(level: ValveLevel):
    """设置全局阀门等级"""
    _current_valve["level"] = level


def get_valve() -> ValveLevel:
    """获取当前阀门等级"""
    return _current_valve["level"]


def check_valve(min_level: ValveLevel):
    """检查当前权限是否满足最低要求，不满足则抛出 PermissionError"""
    current = get_valve()
    if current < min_level:
        names = {0: "READ(只读)", 1: "SUGGEST(建议)", 2: "SEND(发送)"}
        raise PermissionError(
            f"权限不足: 需要 {names.get(min_level, min_level)}, "
            f"当前为 {names.get(current, current)}"
        )


def require_level(min_level: ValveLevel):
    """装饰器：在函数执行前检查权限"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            check_valve(min_level)
            return func(*args, **kwargs)
        return wrapper
    return decorator
