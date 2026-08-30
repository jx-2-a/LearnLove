"""TJU 模型协议的离线回归测试。"""

import json
import unittest
from unittest.mock import patch

from agent.llm import (
    _apply_reasoning_options,
    _prepare_messages,
    _reasoning_text,
    chat_stream,
)
from agent.model_registry import load_registry


class _FakeStreamResponse:
    def __enter__(self):
        """进入伪流式响应上下文。"""
        return self

    def __exit__(self, *_args):
        """退出伪流式响应上下文。"""
        return False

    def raise_for_status(self):
        """模拟成功状态。"""
        return None

    def iter_lines(self):
        """返回 TJU 使用 reasoning 字段的 SSE 数据。"""
        yield 'data: ' + json.dumps({"choices": [{"delta": {"reasoning": "分析"}}]})
        yield 'data: ' + json.dumps({
            "choices": [{"delta": {"content": "回复"}, "finish_reason": "stop"}],
        })
        yield "data: [DONE]"


class _FakeClient:
    last_body = None

    def __init__(self, **_kwargs):
        """接受 httpx.Client 的初始化参数。"""

    def __enter__(self):
        """进入伪客户端上下文。"""
        return self

    def __exit__(self, *_args):
        """退出伪客户端上下文。"""
        return False

    def stream(self, _method, _url, **kwargs):
        """记录请求体并返回伪 SSE 响应。"""
        type(self).last_body = kwargs["json"]
        return _FakeStreamResponse()


class TjuProtocolTests(unittest.TestCase):
    def test_registry_contains_verified_tju_models(self):
        """模型目录应包含三个已验证 TJU 模型。"""
        registry = load_registry()
        self.assertIn("tju-qwen36-35b-a3b", registry.profiles)
        self.assertIn("tju-qwen36-27b", registry.profiles)
        self.assertIn("tju-deepseek-v4-flash", registry.profiles)

    def test_reasoning_fields_are_normalized(self):
        """三种常见思考字段都归一成统一文本。"""
        self.assertEqual(_reasoning_text({"reasoning": "tju"}), "tju")
        self.assertEqual(_reasoning_text({"reasoning_content": "deepseek"}), "deepseek")
        self.assertEqual(_reasoning_text({"thinking_content": "other"}), "other")

    def test_tju_tool_history_uses_reasoning_field(self):
        """内部 reasoning_content 回传 TJU 时应改名为 reasoning。"""
        source = [{"role": "assistant", "content": None, "reasoning_content": "过程"}]
        prepared = _prepare_messages(source, "tju")
        self.assertEqual(prepared[0]["reasoning"], "过程")
        self.assertNotIn("reasoning_content", prepared[0])
        self.assertIn("reasoning_content", source[0])

    def test_tju_stream_parses_reasoning_and_sends_thinking(self):
        """TJU SSE reasoning 应进入思考事件，请求应携带思考开关。"""
        body = {}
        _apply_reasoning_options(body, "tju", "tju", True, "")
        self.assertEqual(body["thinking"], {"type": "enabled"})

        with patch("agent.llm.httpx.Client", _FakeClient):
            events = list(chat_stream(
                [{"role": "user", "content": "你好"}],
                api_base="https://ai.tju.edu.cn/api/v3",
                api_key="tk-test",
                model="qwen3.6-35b-a3b",
                provider="tju",
                protocol="tju",
                thinking=True,
            ))
        self.assertEqual(events[0], {"type": "reasoning", "content": "分析"})
        self.assertEqual(events[1], {"type": "delta", "content": "回复"})
        self.assertEqual(events[-1]["reasoning_content"], "分析")
        self.assertEqual(_FakeClient.last_body["thinking"], {"type": "enabled"})


if __name__ == "__main__":
    unittest.main()
