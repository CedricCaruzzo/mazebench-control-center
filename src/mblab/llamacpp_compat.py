"""Make verifiers' OpenAI chat client speak llama.cpp's dialect.

The bug, precisely: verifiers' `from_chat_message` always populates
`tool_calls` and `reasoning_content` on assistant messages, using `None` when
there are none. The OpenAI API tolerates explicit nulls there. llama-server does
not, and rejects the whole request:

    500 Failed to parse messages:
    [json.exception.type_error.302] type must be string, but is null

It only bites from the SECOND turn onward, because turn one has no assistant
message in the history yet. So a single-shot test passes and a multi-turn agent
fails -- which is the worst possible failure shape for this benchmark.

The fix strips optional keys whose value is None before the request goes out.
For a reasoning-only assistant turn, it also normalizes null content to an empty
string: llama.cpp requires every assistant message to contain `content` or
`tool_calls`, even when `reasoning_content` is populated.
"""

from __future__ import annotations

_PATCHED = False


def clean_native_messages(messages):  # type: ignore[no-untyped-def]
    """Normalize OpenAI-shaped messages to llama.cpp's stricter contract."""
    cleaned = []
    for message in messages:
        normalized = {k: v for k, v in message.items() if v is not None}
        if (
            normalized.get("role") == "assistant"
            and "content" not in normalized
            and "tool_calls" not in normalized
        ):
            normalized["content"] = ""
        cleaned.append(normalized)
    return cleaned


def patch_verifiers_for_llamacpp() -> None:
    """Idempotently strip None-valued message keys on outgoing requests."""
    global _PATCHED
    if _PATCHED:
        return

    from verifiers.clients.openai_chat_completions_client import (
        OpenAIChatCompletionsClient,
    )

    original = OpenAIChatCompletionsClient.to_native_prompt

    async def to_native_prompt(self, messages):  # type: ignore[no-untyped-def]
        native, extra = await original(self, messages)
        return clean_native_messages(native), extra

    OpenAIChatCompletionsClient.to_native_prompt = to_native_prompt  # type: ignore[method-assign]
    _PATCHED = True
