"""LLM provider abstraction.

Wraps Anthropic SDK and litellm so loop.py can consume a single normalized
event stream regardless of which backend serves the request.

Provider selection is driven by model-name prefix:

- ``claude-...``, ``claude-*`` (no slash)  → Anthropic SDK directly
- ``openai/...``, ``deepseek/...``, ``qwen/...`` etc. → litellm

Loop.py consumes :class:`StreamEvent` instances from either provider
and never touches the underlying SDK shapes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal, Protocol

# Model-name prefixes that select the litellm backend. litellm uses the
# same convention (``openai/gpt-4o``, ``deepseek/deepseek-chat``, ...),
# so this list mirrors the providers users are most likely to plug in.
LITELLM_PREFIXES: tuple[str, ...] = (
    "openai/", "deepseek/", "qwen/", "groq/", "mistral/",
    "gemini/", "azure/", "command-r/", "cohere/", "huggingface/",
    "together_ai/", "fireworks_ai/", "anyscale/", "openrouter/",
    "perplexity/", "voyage/", "ai21/", "baseten/", "custom/",
)


def looks_like_litellm(model: str) -> bool:
    """Return True if ``model`` should route through the litellm backend."""
    if not model:
        return False
    return any(model.startswith(p) for p in LITELLM_PREFIXES)


@dataclass
class StreamEvent:
    """Normalized stream event consumed by AgentLoop.

    - ``text_delta``  — partial assistant text. Loop yields it as a
      streaming ``{"type": "text"}`` event to the client.
    - ``tool_use``    — completed tool-call block (accumulated across
      deltas by the provider). Loop dispatches the tool and emits the
      matching tool_result.
    - ``message_stop``— terminal event. Carries ``stop_reason`` and the
      full ``content_blocks`` list (already dumped to JSON-safe dicts)
      so the loop can persist the assistant turn.
    - ``error``       — provider raised mid-stream. ``message`` carries
      the human-readable string; loop decides whether to retry or surface.
    """
    kind: Literal["text_delta", "tool_use", "message_stop", "error"]
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    stop_reason: str | None = None
    content_blocks: list[dict] | None = None
    usage: dict | None = None
    error: Exception | None = None


class LLMProvider(Protocol):
    """Unified streaming interface for AgentLoop."""

    def stream(self, *, model: str, system: str, messages: list[dict],
               tools: list[dict], max_tokens: int) -> Iterator[StreamEvent]:
        ...

    @property
    def provider_name(self) -> str:  # pragma: no cover - trivial
        ...


# ── Anthropic backend ──────────────────────────────────────────────────────

def _dump_block(b: Any) -> dict:
    """Convert an Anthropic SDK content block to a JSON-safe dict.

    Handles three shapes: plain dicts (passthrough), pydantic models
    (model_dump/dict), and plain dataclass / attribute-bag objects
    (dataclasses.asdict + attribute fallback for test mocks).
    """
    if isinstance(b, dict):
        return b
    if hasattr(b, "model_dump"):
        return b.model_dump()
    if hasattr(b, "dict") and callable(getattr(b, "dict")):
        return b.dict()
    import dataclasses
    if dataclasses.is_dataclass(b) and not isinstance(b, type):
        return dataclasses.asdict(b)
    # Last-resort attribute reflection for objects that quack like a
    # content block (type/name/id/text/input fields).
    attrs = {k: getattr(b, k) for k in ("type", "name", "id", "text",
                                        "input", "tool_use_id")
             if hasattr(b, k)}
    if attrs:
        return attrs
    return {"type": "unknown", "repr": repr(b)}


class AnthropicProvider:
    """Wraps the existing anthropic SDK streaming path.

    Re-implements the inline logic that used to live in loop.py —
    opening the stream, forwarding text_delta events, and emitting a
    final ``message_stop`` carrying the complete assistant message.
    """

    provider_name = "anthropic"

    def __init__(self, client_factory: Callable[[], Any]):
        # We build the client lazily so tests can swap the factory
        # without paying the import / network cost up-front.
        self._client_factory = client_factory
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def stream(self, *, model: str, system: str, messages: list[dict],
               tools: list[dict], max_tokens: int) -> Iterator[StreamEvent]:
        # Use the SDK's context-manager form so the underlying HTTP
        # stream is closed deterministically. We yield events while
        # inside the context, then a final message_stop after.
        cm = self.client.messages.stream(
            model=model, system=system, messages=messages,
            tools=tools, max_tokens=max_tokens,
        )
        with cm as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None) if delta else None
                    if dtype == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            yield StreamEvent(kind="text_delta", text=text)
            final = stream.get_final_message()
        # Convert each block to a plain dict so downstream JSON
        # serialization doesn't fall back to default=str on pydantic
        # objects (which would destroy the structure on disk).
        blocks = [_dump_block(b) for b in final.content]
        usage_obj = getattr(final, "usage", None)
        usage = None
        if usage_obj is not None:
            usage = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
                "cache_read_input_tokens":
                    getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens":
                    getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
            }
        yield StreamEvent(
            kind="message_stop",
            stop_reason=getattr(final, "stop_reason", None),
            content_blocks=blocks,
            usage=usage,
        )


# ── litellm backend ────────────────────────────────────────────────────────

class LiteLLMProvider:
    """Adapter for any OpenAI-compatible endpoint via litellm.

    litellm exposes a single ``completion(...)`` API that knows how to
    route to OpenAI, DeepSeek, Qwen, Gemini, Groq, Mistral, Azure, and
    many more based on the ``model=`` prefix. We translate the OpenAI
    chat-completion chunk format back into the normalized StreamEvent
    shape so loop.py doesn't need to know which backend is running.

    Tool-call handling: OpenAI streams tool-call arguments as a series
    of *partial JSON strings* across multiple chunks (delta.tool_calls
    with index-based accumulation). We buffer per-index and only emit
    a single ``tool_use`` event once the JSON for that index parses.
    """

    provider_name = "litellm"

    def __init__(self, *, api_key: str | None, base_url: str | None,
                 extra_headers: dict[str, str] | None = None):
        self._api_key = api_key
        self._base_url = base_url
        self._extra_headers = extra_headers or {}

    def stream(self, *, model: str, system: str, messages: list[dict],
               tools: list[dict], max_tokens: int) -> Iterator[StreamEvent]:
        import litellm  # local import — keeps cold-start fast if unused

        # litellm emits a noisy deprecation warning for every call; mute
        # it so it doesn't pollute our structured logs.
        litellm.suppress_debug_messages = True

        payload_messages = self._convert_messages(system, messages)
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=payload_messages,
            max_tokens=max_tokens,
            stream=True,
        )
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["api_base"] = self._base_url
        if self._extra_headers:
            kwargs["extra_headers"] = self._extra_headers

        # Buffer partial tool-call JSON by OpenAI's chunk index.
        # Each entry holds the accumulated function name + args string.
        tool_buf: dict[int, dict[str, Any]] = {}
        final_blocks: list[dict] = []
        final_text_parts: list[str] = []
        finish_reason: str | None = None
        usage_dict: dict | None = None

        for chunk in litellm.completion(**kwargs):
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                # Some providers emit standalone usage chunks at the end
                # of the stream with no choices.
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage_dict = self._usage_to_dict(u)
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    final_text_parts.append(content)
                    yield StreamEvent(kind="text_delta", text=content)
                # Tool calls arrive as a list of deltas, each carrying
                # an index. We accumulate by index to assemble the full
                # function name + JSON args.
                tc_list = getattr(delta, "tool_calls", None) or []
                for tc in tc_list:
                    idx = getattr(tc, "index", 0) or 0
                    slot = tool_buf.setdefault(idx, {
                        "id": None, "name": None, "args": "",
                    })
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        args_chunk = getattr(fn, "arguments", None)
                        if args_chunk:
                            slot["args"] += args_chunk
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage_dict = self._usage_to_dict(u)

        # Flush buffered tool calls now that the stream is complete.
        for idx in sorted(tool_buf.keys()):
            slot = tool_buf[idx]
            args_str = slot.get("args") or "{}"
            try:
                tool_input = json.loads(args_str) if args_str.strip() else {}
            except json.JSONDecodeError:
                # The model produced malformed JSON — surface it to the
                # tool layer as a single-arg error so the model can
                # recover rather than crashing the whole turn.
                tool_input = {"_malformed_args": args_str}
            block_id = slot.get("id") or f"call_{idx}"
            block_name = slot.get("name") or "unknown"
            final_blocks.append({
                "type": "tool_use",
                "id": block_id,
                "name": block_name,
                "input": tool_input,
            })
            yield StreamEvent(
                kind="tool_use",
                tool_call_id=block_id,
                tool_name=block_name,
                tool_input=tool_input,
            )

        # If any text was emitted, prepend a text block so the persisted
        # assistant message mirrors Anthropic's text+tool_use layout.
        if final_text_parts:
            final_blocks.insert(0, {"type": "text", "text": "".join(final_text_parts)})

        # Map OpenAI finish_reason → Anthropic stop_reason so the loop's
        # max_tokens escalation continues to work.
        stop_reason = self._map_stop_reason(finish_reason)

        yield StreamEvent(
            kind="message_stop",
            stop_reason=stop_reason,
            content_blocks=final_blocks,
            usage=usage_dict,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _map_stop_reason(fr: str | None) -> str:
        # OpenAI: "stop", "length", "tool_calls", "content_filter"
        # Anthropic: "end_turn", "max_tokens", "tool_use", "stop_sequence"
        if fr == "length":
            return "max_tokens"
        if fr == "tool_calls":
            return "tool_use"
        if fr == "stop":
            return "end_turn"
        if fr is None:
            return "end_turn"
        return fr

    @staticmethod
    def _usage_to_dict(u: Any) -> dict:
        return {
            "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(u, "completion_tokens", 0) or 0,
            # OpenAI doesn't report cache stats; zero keeps the metrics
            # counters happy without a separate code path.
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    @staticmethod
    def _convert_messages(system: str, messages: list[dict]) -> list[dict]:
        """Anthropic-format → OpenAI chat-format.

        - system string is hoisted out (Anthropic takes it separately).
        - assistant content blocks (text + tool_use) collapse to a
          ``content`` string + ``tool_calls`` array.
        - user tool_result content blocks become a separate user message
          with role=tool + tool_call_id.
        """
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                continue
            if role == "assistant":
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                for b in content:
                    bt = b.get("type") if isinstance(b, dict) else None
                    if bt == "text":
                        text_parts.append(b.get("text", ""))
                    elif bt == "tool_use":
                        tool_calls.append({
                            "id": b.get("id"),
                            "type": "function",
                            "function": {
                                "name": b.get("name"),
                                "arguments": json.dumps(b.get("input") or {}),
                            },
                        })
                msg: dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    msg["content"] = "".join(text_parts)
                else:
                    msg["content"] = None
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
            elif role == "user":
                # User message may be a list of tool_results OR plain text.
                # OpenAI expects each tool_result as its own top-level
                # ``role: tool`` message.
                plain: list[str] = []
                tool_results: list[dict] = []
                for b in content:
                    bt = b.get("type") if isinstance(b, dict) else None
                    if bt == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            # Anthropic allows content blocks here; flatten
                            # to a string for OpenAI compatibility.
                            c = "".join(x.get("text", "") if isinstance(x, dict) else str(x)
                                        for x in c)
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": b.get("tool_use_id"),
                            "content": str(c) if c is not None else "",
                        })
                    elif bt == "text":
                        plain.append(b.get("text", ""))
                    elif isinstance(b, str):
                        plain.append(b)
                if tool_results:
                    out.extend(tool_results)
                if plain:
                    out.append({"role": "user", "content": "".join(plain)})
        return out

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """Anthropic tool schema → OpenAI function-tool schema."""
        out: list[dict] = []
        for t in tools:
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {
                        "type": "object", "properties": {}},
                },
            })
        return out


# ── Selector ───────────────────────────────────────────────────────────────

def select_provider(model: str, cfg) -> LLMProvider:
    """Return the right provider for the configured model.

    Auto-detected by model-name prefix per :data:`LITELLM_PREFIXES`.
    Falls back to the Anthropic SDK so existing deployments using
    bare ``claude-sonnet-4-6`` or DeepSeek's anthropic-compatible
    endpoint keep working unchanged.
    """
    if looks_like_litellm(model):
        return LiteLLMProvider(
            api_key=cfg.litellm_api_key,
            base_url=cfg.litellm_base_url,
        )
    return AnthropicProvider(cfg.build_client)
