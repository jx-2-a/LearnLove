"""匿名审计真实微信库的解析覆盖率，不输出联系人或聊天正文。"""

import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import load_config
from agent.paths import config_path
from agent.tools._state import state
from agent.tools.message import _find_msg_table, _get_sender_map, _query_messages
from agent.wechat_parser import _xml_root, normalize_message


def _contact_key(wxid: str, index: int) -> str:
    """生成不可逆的短标识，避免审计输出联系人信息。"""
    digest = hashlib.sha256(wxid.encode("utf-8")).hexdigest()[:8]
    return f"contact#{index}:{digest}"


def audit(sample_per_table: int = 500) -> dict:
    """统计发送者、类型、引用、重复和顺序覆盖率。"""
    config = load_config(config_path())
    state.config = config
    wechat = config.get("wechat", {})
    state.setup(wechat["db_dir"], wechat["keys_file"])
    state.load_contacts()
    reports = []
    try:
        for index, configured in enumerate(state.contacts_config(), start=1):
            wxid = configured.get("wxid", "")
            display = state.contacts.get(wxid, {}).get("display", wxid)
            messages = []
            table_count = 0
            for db_path, table_name, source_db in _find_msg_table(wxid):
                table_count += 1
                conn = sqlite3.connect(db_path)
                try:
                    sender_map = _get_sender_map(
                        conn, table_name, wxid, display, db_path
                    )
                    rows = _query_messages(
                        conn, table_name, limit=sample_per_table
                    )
                finally:
                    conn.close()
                messages.extend(
                    normalize_message(
                        row, wxid, display, sender_map, source_db, table_name
                    )
                    for row in rows
                )

            ids = [message["message_id"] for message in messages]
            unique = {message["message_id"]: message for message in messages}
            ordered = sorted(
                unique.values(),
                key=lambda message: (
                    message["create_time"],
                    message.get("sort_seq", 0),
                    message.get("server_seq", 0),
                    message.get("local_id", 0),
                    message["message_id"],
                ),
            )
            roles = Counter(message["sender_role"] for message in ordered)
            kinds = Counter(message["type"] for message in ordered)
            app_types = Counter(
                message.get("metadata", {}).get("app_type", 0)
                for message in ordered
                if message.get("local_type") == 49
            )
            app_zero_shapes = Counter()
            app_zero_xml_parse_failures = 0
            for message in ordered:
                if (
                    message.get("local_type") != 49
                    or message.get("metadata", {}).get("app_type", 0)
                ):
                    continue
                raw = str(message.get("raw_content") or "").lstrip()
                if _xml_root(raw) is None:
                    app_zero_xml_parse_failures += 1
                if not raw:
                    shape = "empty"
                elif "<appmsg" in raw:
                    shape = "appmsg_without_numeric_type"
                elif raw.startswith("<"):
                    shape = "xml_without_appmsg"
                else:
                    shape = "non_xml"
                app_zero_shapes[shape] += 1
            quote_count = sum(bool(message.get("quote")) for message in ordered)
            quote_missing = sum(
                bool(message.get("quote"))
                and not message.get("quote", {}).get("content")
                for message in ordered
            )
            unknown_types = sum(
                str(message.get("type", "")).startswith("未知类型")
                for message in ordered
            )
            unresolved = Counter(
                (message.get("local_type", 0), message.get("sender_id"))
                for message in ordered
                if message.get("sender_role") == "unknown"
            )
            reports.append({
                "contact": _contact_key(wxid, index),
                "tables": table_count,
                "sampled_rows": len(messages),
                "unique_messages": len(ordered),
                "cross_db_duplicates": len(ids) - len(set(ids)),
                "sender_roles": dict(roles),
                "sender_resolution_percent": round(
                    100 * (len(ordered) - roles.get("unknown", 0))
                    / max(1, len(ordered)),
                    2,
                ),
                "message_types": dict(kinds),
                "app_subtypes": {
                    str(key): value for key, value in app_types.items()
                },
                "app_type_zero_shapes": dict(app_zero_shapes),
                "app_type_zero_xml_parse_failures": app_zero_xml_parse_failures,
                "quotes": quote_count,
                "quotes_without_text": quote_missing,
                "unknown_types": unknown_types,
                "unresolved_shapes": [
                    {"local_type": key[0], "sender_id": key[1], "count": count}
                    for key, count in unresolved.items()
                ],
                "chronological": all(
                    ordered[pos]["create_time"] <= ordered[pos + 1]["create_time"]
                    for pos in range(len(ordered) - 1)
                ),
            })
    finally:
        if state.db_cache:
            state.db_cache.cleanup()
    return {
        "format": "learnlove.parser.audit.v1",
        "sample_per_table": sample_per_table,
        "contacts": reports,
    }


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
