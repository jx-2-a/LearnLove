"""
溢出输出管理 — 大结果写入磁盘，避免静默截断

核心原则：永不静默截断。超过限制的输出写入磁盘，
返回带 id 的截断标记，LLM 可通过 view_output 检索完整内容。
"""

import os
import re
import tempfile
from datetime import datetime

from agent.paths import spills_dir

SPILL_DIR = spills_dir()
os.makedirs(SPILL_DIR, exist_ok=True)

_counter = [0]  # 简单计数器（串行，无需锁）


def _next_id() -> str:
    _counter[0] += 1
    return f"L{_counter[0]}"


def spill(text: str, source: str = "") -> dict:
    """将完整文本写入磁盘，返回 id 和元数据"""
    oid = _next_id()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{oid}.txt"
    filepath = os.path.join(SPILL_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(text)
    lines = text.count("\n") + 1
    return {
        "id": oid,
        "path": filepath,
        "lines": lines,
        "chars": len(text),
    }


def clip(text: str, limit: int = 4000, source: str = "", keep: str = "head") -> str:
    """截断文本。超出 limit 时溢出到磁盘，返回带标记的文本。

    keep="head": 保留开头，标记末尾截断
    keep="tail": 保留结尾，标记开头截断
    """
    if len(text) <= limit:
        return text

    info = spill(text, source)
    marker = (
        f"\n\n…[截断: 完整输出共 {info['lines']} 行 {info['chars']} 字符, "
        f"id={info['id']}, 用 view_output({info['id']}) 查看]…"
    )

    if keep == "head":
        # 为标记留出空间
        cut = limit - len(marker) - 50
        return text[:cut] + marker
    else:
        cut = len(text) - limit + len(marker) + 50
        return marker + "\n" + text[cut:]


def clip_list(items: list, limit_items: int = 20, render=str, source: str = "") -> str:
    """截断列表。超出 limit_items 时溢出完整列表到磁盘。"""
    if len(items) <= limit_items:
        return "\n".join(render(item) for item in items)

    shown = items[:limit_items]
    full_text = "\n".join(render(item) for item in items)
    info = spill(full_text, source)
    result = "\n".join(render(item) for item in shown)
    result += (
        f"\n…[截断: 共 {len(items)} 项, 仅显示前 {limit_items}, "
        f"id={info['id']}, 用 view_output({info['id']}) 查看完整列表]…"
    )
    return result


def view(oid: str, start: int = None, end: int = None, grep: str = None) -> dict:
    """检索溢出的输出内容

    Args:
        oid: 溢出 id (如 "L3")
        start: 起始行号 (1-based)
        end: 结束行号 (1-based, inclusive)
        grep: 过滤包含该字符串的行
    """
    # 查找文件
    for f in os.listdir(SPILL_DIR):
        if oid in f:
            filepath = os.path.join(SPILL_DIR, f)
            with open(filepath, "r", encoding="utf-8") as fh:
                lines = fh.readlines()

            if grep:
                lines = [l for l in lines if grep in l]

            if start is not None or end is not None:
                s = (start or 1) - 1
                e = end if end is not None else len(lines)
                lines = lines[s:e]

            text = "".join(lines)
            total_lines = len(text.split("\n"))
            return {
                "ok": True,
                "data": {
                    "id": oid,
                    "lines": total_lines,
                    "chars": len(text),
                    "text": text,
                },
            }

    return {"ok": False, "error": f"未找到溢出输出: {oid}"}
