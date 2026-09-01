"""微信规范化解析与 Agent 时间线契约测试。"""

import unittest

from agent.chat_timeline import build_agent_timeline
from agent.wechat_parser import normalize_message, parse_content, stable_message_id


def raw_message(local_type=1, content="你好", sender_id=1):
    """构造包含稳定排序字段的微信数据库行。"""
    return {
        "create_time": 1720000000,
        "local_type": local_type,
        "local_id": 7,
        "server_id": 123456,
        "sort_seq": 8,
        "server_seq": 9,
        "sender_id": sender_id,
        "content": content,
        "packed_info_data": b"",
    }


class WeChatParserTests(unittest.TestCase):
    """覆盖身份、引用、常见类型和未知事实边界。"""

    def test_stable_id_prefers_server_id(self):
        """服务器 ID 存在时不再依赖易冲突的正文和时间。"""
        self.assertEqual(
            stable_message_id(raw_message(), "message_0.db", "Msg_x"),
            "wx:123456",
        )

    def test_sender_roles_are_explicit_and_unknown_is_not_guessed(self):
        """联系人、本人和未知发送者必须是三个不同状态。"""
        contact = normalize_message(
            raw_message(sender_id=1),
            "wxid_contact",
            "小明",
            {1: "wxid_contact", 2: "__self__"},
            "message_0.db",
            "Msg_x",
        )
        own = normalize_message(
            raw_message(sender_id=2),
            "wxid_contact",
            "小明",
            {1: "wxid_contact", 2: "__self__"},
            "message_0.db",
            "Msg_x",
        )
        unknown = normalize_message(
            raw_message(sender_id=3),
            "wxid_contact",
            "小明",
            {},
            "message_0.db",
            "Msg_x",
        )
        self.assertEqual((contact["sender_role"], contact["sender"]), ("contact", "小明"))
        self.assertEqual((own["sender_role"], own["sender"]), ("self", "我"))
        self.assertFalse(unknown["sender_resolved"])
        self.assertEqual(unknown["sender_role"], "unknown")

    def test_quote_body_and_evidence_are_separated(self):
        """新正文与被引用内容不能拼成一段让 Agent 混淆。"""
        xml = """<msg><appmsg><title>我觉得可以</title><des></des><type>57</type>
        <refermsg><type>1</type><svrid>9988</svrid><displayname>小明</displayname>
        <content>那周六见？</content></refermsg></appmsg></msg>"""
        kind, body, quote, metadata = parse_content(49, xml)
        self.assertEqual(kind, "引用")
        self.assertEqual(body, "我觉得可以")
        self.assertEqual(quote["content"], "那周六见？")
        self.assertEqual(quote["server_id"], "9988")
        self.assertEqual(metadata["app_type"], 57)
        self.assertNotIn("那周六见", body)

    def test_xml_parser_removes_illegal_control_characters(self):
        """微信字段中的控制字符不能让整条应用消息退化为未分类。"""
        xml = (
            '<msg><appmsg><title>链接标题</title><type>5</type>'
            '<des>说明\x00文本</des></appmsg></msg>'
        )
        kind, body, _, metadata = parse_content(49, xml)
        self.assertEqual(kind, "链接")
        self.assertEqual(metadata["app_type"], 5)
        self.assertEqual(body, "链接标题")

    def test_unknown_app_subtype_remains_visible(self):
        """未知应用子类型保留编号，不能笼统丢成同一个标签。"""
        kind, body, _, metadata = parse_content(
            49,
            '<msg><appmsg><title>内容</title><type>8</type></appmsg></msg>',
        )
        self.assertEqual(kind, "应用消息(8)")
        self.assertEqual(body, "内容")
        self.assertEqual(metadata["app_type"], 8)

    def test_common_media_and_location_types(self):
        """图片、语音和位置保留类型及关键元数据。"""
        kind, body, _, image = parse_content(
            3, '<msg><img aeskey="abc" md5="aabb" length="42"/></msg>'
        )
        self.assertEqual((kind, body), ("图片", "[图片]"))
        self.assertEqual(image["md5"], "aabb")

        kind, body, _, voice = parse_content(
            34, '<msg><voicemsg voicelength="3210" voiceformat="silk"/></msg>'
        )
        self.assertEqual(kind, "语音")
        self.assertEqual(voice["duration_ms"], 3210)

        kind, body, _, location = parse_content(
            48, '<msg><location poiname="天津大学" label="卫津路"/></msg>'
        )
        self.assertEqual((kind, body), ("位置", "[位置] 天津大学"))
        self.assertEqual(location["label"], "卫津路")

    def test_system_message_is_not_misclassified_as_contact(self):
        """微信系统消息不能被当作对方发言。"""
        message = normalize_message(
            raw_message(local_type=10000, content="你已添加了对方", sender_id=3),
            "wxid_contact",
            "小明",
            {},
            "message_0.db",
            "Msg_x",
        )
        self.assertEqual(message["sender_role"], "system")
        self.assertEqual(message["sender"], "微信系统")
        self.assertTrue(message["sender_resolved"])

    def test_agent_timeline_is_chronological_and_diagnostic(self):
        """时间线明确角色、引用、媒体状态和不确定项。"""
        messages = [
            {
                "message_id": "wx:1",
                "time": "08-31 10:00",
                "create_time": 1,
                "sender_role": "self",
                "sender": "我",
                "is_self": True,
                "type": "文本",
                "content": "早",
                "quote": {},
            },
            {
                "message_id": "wx:2",
                "time": "08-31 10:01",
                "create_time": 2,
                "sender_role": "contact",
                "sender": "小明",
                "is_self": False,
                "type": "引用",
                "content": "好呀",
                "quote": {
                    "sender": "我",
                    "content": "周六见吗",
                    "type": 1,
                    "server_id": "1",
                },
            },
            {
                "message_id": "wx:3",
                "time": "08-31 10:02",
                "create_time": 3,
                "sender_role": "unknown",
                "sender": "未知发送者(sid=9)",
                "is_self": False,
                "type": "图片",
                "content": "[图片]",
                "media": {"kind": "image", "status": "pending", "result": ""},
            },
        ]
        view = build_agent_timeline(list(reversed(messages)), "小明")
        self.assertEqual(view["format"], "learnlove.chat.timeline.v1")
        self.assertEqual([item["id"] for item in view["records"]], ["wx:1", "wx:2", "wx:3"])
        self.assertIn("↳ 引用 我", view["transcript"])
        self.assertIn("媒体识别 pending", view["transcript"])
        self.assertEqual(view["diagnostics"]["speaker_counts"]["unknown"], 1)
        self.assertEqual(len(view["diagnostics"]["warnings"]), 2)


if __name__ == "__main__":
    unittest.main()
