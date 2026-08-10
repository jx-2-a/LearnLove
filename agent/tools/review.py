"""
复盘报告工具 — list_reviews / read_review

供咨询模式回顾历史复盘报告。报告文件保存在 {USER_DATA_DIR}/reviews/ 下，
文件名格式: {联系人}_{YYYYMMDD}_{HHMMSS}.md（由 chat.py 的 /review 生成）。
"""

import os
import re
from datetime import datetime

from agent.protocol import ok, err
from agent.paths import reviews_dir
from agent.outputs import clip

# 文件名格式: {contact}_{YYYYMMDD}_{HHMMSS}.md
# 联系人名可能含下划线，用末尾的时间戳反推，避免把联系人名拆错
_FILENAME_RE = re.compile(r"^(.*?)_(\d{8})_(\d{6})\.md$")


def _parse_filename(fname: str) -> dict:
    """从文件名解析联系人和时间。"""
    m = _FILENAME_RE.match(fname)
    if not m:
        return {"contact": "", "time": ""}
    contact = m.group(1)
    try:
        ts = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S")
        time_str = ts.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        time_str = ""
    return {"contact": contact, "time": time_str}


def _scan_reviews() -> list[dict]:
    """扫描 reviews 目录，返回按时间倒序的报告列表。"""
    d = reviews_dir()
    files = []
    if not os.path.isdir(d):
        return files
    for fname in os.listdir(d):
        if not fname.endswith(".md"):
            continue
        meta = _parse_filename(fname)
        files.append({
            "filename": fname,
            "path": os.path.join(d, fname),
            "contact": meta["contact"],
            "time": meta["time"],
        })
    files.sort(key=lambda f: f["time"], reverse=True)
    return files


def list_reviews(contact_name: str = "", limit: int = 20) -> dict:
    """列出复盘报告文件"""
    files = _scan_reviews()
    if contact_name:
        files = [f for f in files if f["contact"] == contact_name]
    files = files[:max(1, min(limit, 50))]
    return ok({"reviews": files, "count": len(files)})


def read_review(filename: str = "", contact_name: str = "", latest: bool = False) -> dict:
    """读取复盘报告内容。

    提供 filename 精确读取，或 contact_name + latest=true 读取该联系人最新报告。
    """
    if not filename and not (contact_name and latest):
        return err("请提供 filename，或同时提供 contact_name 且 latest=true")
    files = _scan_reviews()
    if not files:
        return err("还没有复盘报告，先运行 /review 生成")

    target = None
    if filename:
        for f in files:
            if f["filename"] == filename:
                target = f
                break
        if target is None:
            return err(f"未找到报告: {filename}，可用 list_reviews 查看")
    else:
        candidates = [f for f in files if f["contact"] == contact_name]
        if not candidates:
            return err(f"没有 {contact_name} 的复盘报告")
        target = candidates[0]  # 已按时间倒序，即最新

    try:
        with open(target["path"], "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return err(f"读取失败: {e}")

    return ok({
        "filename": target["filename"],
        "contact": target["contact"],
        "time": target["time"],
        "content": clip(content, 8000),
    })


def get_review_tools() -> list[dict]:
    """返回复盘工具的 OpenAI function calling schema（供咨询模式合并使用）"""
    return [
        {
            "type": "function",
            "function": {
                "name": "list_reviews",
                "description": "列出所有复盘报告文件（含联系人和时间）。咨询模式下需要回顾历史复盘时，先用它查看有哪些报告",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_name": {"type": "string", "description": "只列出该联系人的报告，不指定则列出全部"},
                        "limit": {"type": "integer", "description": "返回数量上限，默认 20"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_review",
                "description": "读取一份复盘报告的完整内容。咨询讨论时需要回顾某次复盘的具体结论时使用",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "报告文件名，如 '谢雨欣_20260808_120000.md'，来自 list_reviews 返回结果"},
                        "contact_name": {"type": "string", "description": "联系人名称，与 latest=true 搭配读取该联系人最新的报告"},
                        "latest": {"type": "boolean", "description": "为 true 且提供 contact_name 时，读取该联系人最新的一份报告"},
                    },
                    "required": [],
                },
            },
        },
    ]
