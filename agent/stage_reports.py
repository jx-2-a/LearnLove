"""版本化阶段分析档案：事实、解释与修订历史分层保存。"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from agent.paths import analysis_stage_dir


def _now() -> str:
    """返回带时区的可排序时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stage_path(contact_name: str, stage_id: str) -> Path:
    """获取阶段 JSON 的稳定路径。"""
    return Path(analysis_stage_dir(contact_name)) / f"stage_{stage_id}.json"


def _render(data: dict) -> str:
    """把当前阶段版本渲染为易读 Markdown。"""
    lines = [f"# {data['title']}", "", f"- 阶段 ID：{data['stage_id']}",
             f"- 联系人：{data['contact']}", f"- 时间范围：{data['start']} ～ {data['end']}",
             f"- 当前版本：v{data['revision']}", f"- 更新：{data['updated_at']}", "", "## 可复核事实"]
    lines.extend(f"- {item}" for item in data["facts"])
    lines += ["", "## 暂定解释（非事实）", data["interpretation"] or "- 尚未形成解释", "", "## 不确定项"]
    lines.extend(f"- {item}" for item in data["uncertainties"])
    lines += ["", "## 下次核对", data["next_check"] or "- 待补充"]
    return "\n".join(lines) + "\n"


def save_stage_report(contact_name: str, title: str, start: str, end: str,
                      facts: list[str], interpretation: str = "", uncertainties: list[str] = None,
                      next_check: str = "", stage_id: str = "", revision_reason: str = "") -> dict:
    """创建或修订阶段；修订前完整快照，永不覆盖旧判断。"""
    root = Path(analysis_stage_dir(contact_name))
    root.mkdir(parents=True, exist_ok=True)
    stage_id = stage_id.strip() or f"stg_{uuid.uuid4().hex[:12]}"
    target = _stage_path(contact_name, stage_id)
    now = _now()
    previous = None
    if target.exists():
        previous = json.loads(target.read_text(encoding="utf-8"))
        history = root / "history" / stage_id
        history.mkdir(parents=True, exist_ok=True)
        (history / f"v{previous['revision']}.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    data = {"stage_id": stage_id, "contact": contact_name, "title": title or (previous or {}).get("title", "关系阶段分析"),
            "start": start or (previous or {}).get("start", ""), "end": end or (previous or {}).get("end", ""),
            "facts": [item for item in facts if item.strip()], "interpretation": interpretation,
            "uncertainties": [item for item in (uncertainties or []) if item.strip()], "next_check": next_check,
            "revision": int((previous or {}).get("revision", 0)) + 1, "created_at": (previous or {}).get("created_at", now),
            "updated_at": now, "revision_reason": revision_reason}
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = root / f"stage_{stage_id}.md"
    markdown.write_text(_render(data), encoding="utf-8")
    return {"contact": contact_name, "stage_id": stage_id, "revision": data["revision"], "updated": bool(previous), "path": str(markdown)}


def list_stage_reports(contact_name: str, limit: int = 20) -> list[dict]:
    """列出版本化阶段，旧 Markdown 仍以只读兼容项显示。"""
    root = Path(analysis_stage_dir(contact_name))
    if not root.is_dir(): return []
    reports = []
    for item in sorted(root.glob("stage_*.json"), key=lambda value: value.stat().st_mtime, reverse=True)[:limit]:
        data = json.loads(item.read_text(encoding="utf-8"))
        reports.append({key: data.get(key) for key in ("stage_id", "title", "start", "end", "revision", "updated_at", "revision_reason")})
    return reports


def read_stage_report(contact_name: str, stage_id: str) -> dict:
    """读取当前版本以及可用修订版本号。"""
    target = _stage_path(contact_name, stage_id)
    if not target.exists(): raise FileNotFoundError("未找到阶段 ID")
    data = json.loads(target.read_text(encoding="utf-8"))
    history = target.parent / "history" / stage_id
    data["history_versions"] = sorted(path.stem for path in history.glob("v*.json")) if history.is_dir() else []
    return data
