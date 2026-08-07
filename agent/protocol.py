"""
消息信封 — 所有工具返回统一的 {ok, data, error} 格式
"""

from typing import Any


def ok(data: Any = None) -> dict:
    """成功响应"""
    return {"ok": True, "data": data}


def err(error: str) -> dict:
    """错误响应"""
    return {"ok": False, "error": error}
