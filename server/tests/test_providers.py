"""OpenAI-compatible and Anthropic adapters against recorded SSE streams (no network)."""

import json

import httpx
import httpx2
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from app.providers.anthropic_provider import AnthropicProvider, to_anthropic_messages
from app.providers.base import ChatMessage, TextDelta, ToolCall, ToolCallEvent, ToolSpec, UsageEvent
from app.providers.openai_compat import OpenAICompatProvider, to_openai_messages

TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})]


def sse(events: list[str]) -> bytes:
    return "".join(events).encode()


def openai_chunk(delta: dict, finish=None, usage=None) -> str:
    body = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if delta is not None else []}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


async def collect(provider, messages, tools=TOOLS):
    return [ev async for ev in provider.stream("m", "be helpful", messages, tools)]


async def test_openai_stream_text_tools_usage_and_openrouter_cost():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        stream = sse([
            openai_chunk({"role": "assistant", "content": "Let me "}),
            openai_chunk({"content": "look."}),
            openai_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"pa'}}]}),
            openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}),
            openai_chunk({}, finish="tool_calls"),
            openai_chunk(None, usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": 0.0042}),
            "data: [DONE]\n\n",
        ])
        return httpx.Response(200, content=stream, headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(api_key="k", base_url="https://openrouter.ai/api/v1")
    p.client = AsyncOpenAI(api_key="k", base_url="https://openrouter.ai/api/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    history = [
        ChatMessage("user", "hi"),
        ChatMessage("assistant", "", tool_calls=[ToolCall("old", "read_file", {"path": "x"})]),
        ChatMessage("tool", "content of x", tool_call_id="old"),
    ]
    events = await collect(p, history)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert text == "Let me look."
    assert calls == [ToolCall("call_1", "read_file", {"path": "a.py"})]
    assert (usage.input_tokens, usage.output_tokens, usage.cost, usage.estimated) == (120, 30, 0.0042, False)
    body = seen["body"]
    assert body["stream_options"] == {"include_usage": True} and body["usage"] == {"include": True}
    assert body["messages"][0] == {"role": "system", "content": "be helpful"}
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"path": "x"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "old", "content": "content of x"}
    assert body["tools"][0]["function"]["name"] == "read_file"


async def test_openai_without_usage_is_estimated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse([openai_chunk({"content": "x" * 40}), "data: [DONE]\n\n"]),
                              headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(api_key="k", base_url="http://local/v1")
    p.client = AsyncOpenAI(api_key="k", base_url="http://local/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    usage = next(e for e in await collect(p, [ChatMessage("user", "hello there")], []) if isinstance(e, UsageEvent))
    assert usage.estimated and usage.output_tokens == 10


def anthropic_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"


async def test_anthropic_stream_text_tool_use_and_usage():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        stream = sse([
            anthropic_event("message_start", {"message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": [],
                                                          "stop_reason": None, "stop_sequence": None,
                                                          "usage": {"input_tokens": 200, "cache_read_input_tokens": 50, "output_tokens": 1}}}),
            anthropic_event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            anthropic_event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Reading "}}),
            anthropic_event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "it."}}),
            anthropic_event("content_block_stop", {"index": 0}),
            anthropic_event("content_block_start", {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}}),
            anthropic_event("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path": '}}),
            anthropic_event("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '"b.py"}'}}),
            anthropic_event("content_block_stop", {"index": 1}),
            anthropic_event("message_delta", {"delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}}),
            anthropic_event("message_stop", {}),
        ])
        return httpx2.Response(200, content=stream, headers={"content-type": "text/event-stream"})

    p = AnthropicProvider(api_key="k", base_url=None)
    p.client = AsyncAnthropic(api_key="k", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    history = [
        ChatMessage("user", "q1"),
        ChatMessage("user", "[Other said]: idea"),
        ChatMessage("assistant", "checking", tool_calls=[ToolCall("t0", "read_file", {"path": "a"})]),
        ChatMessage("tool", "A", tool_call_id="t0"),
    ]
    events = await collect(p, history)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Reading it."
    assert [e.call for e in events if isinstance(e, ToolCallEvent)] == [ToolCall("toolu_1", "read_file", {"path": "b.py"})]
    usage = next(e for e in events if isinstance(e, UsageEvent))
    assert (usage.input_tokens, usage.output_tokens) == (250, 42)
    body = seen["body"]
    assert body["system"] == "be helpful" and body["tools"][0]["input_schema"]["type"] == "object"
    # consecutive user turns merged; tool result sent as a user tool_result block
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert len(body["messages"][0]["content"]) == 2
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "t0", "content": "A"}


def test_message_conversions_edge_cases():
    assert to_openai_messages("", [ChatMessage("assistant", "")])[0] == {"role": "assistant", "content": ""}
    assert to_anthropic_messages([ChatMessage("assistant", "")])[0]["content"] == [{"type": "text", "text": "(no content)"}]


async def test_effort_is_sent_in_each_vendors_format():
    bodies = []

    def oa_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=sse([openai_chunk({"content": "ok"}), "data: [DONE]\n\n"]),
                              headers={"content-type": "text/event-stream"})

    for base in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1"):
        p = OpenAICompatProvider(api_key="k", base_url=base)
        p.client = AsyncOpenAI(api_key="k", base_url=base, http_client=httpx.AsyncClient(transport=httpx.MockTransport(oa_handler)))
        _ = [e async for e in p.stream("m", "", [ChatMessage("user", "hi")], [], effort="xhigh")]
    assert bodies[0]["reasoning"] == {"effort": "high"} and "reasoning_effort" not in bodies[0]  # OpenRouter
    assert bodies[1]["reasoning_effort"] == "high" and "reasoning" not in bodies[1]  # OpenAI
    p = OpenAICompatProvider(api_key="k", base_url="https://api.openai.com/v1")
    p.client = AsyncOpenAI(api_key="k", base_url="https://api.openai.com/v1", http_client=httpx.AsyncClient(transport=httpx.MockTransport(oa_handler)))
    _ = [e async for e in p.stream("m", "", [ChatMessage("user", "hi")], [])]
    assert "reasoning_effort" not in bodies[2]  # default: nothing sent

    seen = []

    def an_handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, content=sse([
            anthropic_event("message_start", {"message": {"id": "m", "type": "message", "role": "assistant", "model": "m", "content": [],
                                                          "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}),
            anthropic_event("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
            anthropic_event("message_stop", {}),
        ]), headers={"content-type": "text/event-stream"})

    p = AnthropicProvider(api_key="k", base_url=None)
    p.client = AsyncAnthropic(api_key="k", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(an_handler)))
    _ = [e async for e in p.stream("claude-opus-5-5", "", [ChatMessage("user", "hi")], [], effort="max")]
    _ = [e async for e in p.stream("claude-opus-4-5", "", [ChatMessage("user", "hi")], [], effort="max")]
    assert seen[0]["output_config"] == {"effort": "max"}
    assert seen[1]["output_config"] == {"effort": "high"}  # Opus 4.5 tops out at high


def test_fit_to_window_keeps_newest_and_tool_pairs():
    from app.agent.context import fit_to_window

    big = "x" * 8000  # ~2000 tokens
    history = [
        ChatMessage("user", "q1 " + big),
        ChatMessage("assistant", "a1 " + big),
        ChatMessage("user", "q2"),
        ChatMessage("assistant", "", tool_calls=[ToolCall("c1", "read_file", {"path": "x"})]),
        ChatMessage("tool", "result " + big, tool_call_id="c1"),
        ChatMessage("assistant", "a2"),
        ChatMessage("user", "q3"),
    ]
    kept, omitted = fit_to_window("system", history, 5000)  # 1250 reserved for the reply
    assert omitted == 2  # q1 and a1 don't fit; the tool call and its big result stay together
    assert kept[0].role == "user" and "2 earlier messages were left out" in kept[0].content and "q2" in kept[0].content
    assert [m.role for m in kept[1:]] == ["assistant", "tool", "assistant", "user"]
    kept, omitted = fit_to_window("system", history, 2500)  # now the tool call no longer fits either
    assert omitted == 5 and [m.role for m in kept] == ["user", "assistant", "user"]
    assert fit_to_window("system", history, 1_000_000) == (history, 0)
    # the newest exchange is kept even when it alone is too big
    kept, omitted = fit_to_window("system", [ChatMessage("user", big * 10)], 4096)
    assert omitted == 0 and len(kept) == 1
