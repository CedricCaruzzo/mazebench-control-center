"""Durable, lossless journals for model interactions.

MazeBench keeps the assistant response in its in-memory trajectory, but its
rendered completion contains only environment observations.  This module taps
the native OpenAI-compatible response before that projection discards fields
such as ``reasoning_content``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def jsonable(value: Any) -> Any:
    """Convert Pydantic/OpenAI values into JSON-compatible Python values."""
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prompt_digest(prompt: list[Any]) -> str:
    encoded = json.dumps(prompt, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _shared_message_prefix(previous: list[Any], current: list[Any]) -> int:
    shared = 0
    for old, new in zip(previous, current):
        if old != new:
            break
        shared += 1
    return shared


class InteractionJournal:
    """Append one record per native model call and fsync it for live analysis."""

    def __init__(
        self,
        path: Path,
        *,
        call_offset: int = 0,
        previous_prompt: list[Any] | None = None,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._call_index = max(0, int(call_offset))
        self._previous_prompt = jsonable(previous_prompt or [])

    def begin(
        self,
        *,
        prompt: list[Any],
        model: str,
        sampling_args: dict[str, Any],
        tools: list[Any] | None,
    ) -> dict[str, Any]:
        native_prompt = jsonable(prompt)
        if not isinstance(native_prompt, list):
            native_prompt = []
        with self._lock:
            self._call_index += 1
            call_index = self._call_index
            shared = _shared_message_prefix(self._previous_prompt, native_prompt)
            self._previous_prompt = native_prompt
        return {
            "schema_version": 1,
            "call": call_index,
            "started_at": utc_now(),
            "_started_monotonic": time.perf_counter(),
            "request": {
                "model": model,
                "message_count": len(native_prompt),
                "shared_prefix_messages": shared,
                # Applying each delta in call order reconstructs the exact prompt.
                "appended_messages": native_prompt[shared:],
                "prompt_sha256": _prompt_digest(native_prompt),
                "sampling_args": jsonable(sampling_args),
                "tools": jsonable(tools) if tools else None,
            },
        }

    def finish(
        self,
        pending: dict[str, Any],
        *,
        response: Any | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        started = float(pending.pop("_started_monotonic"))
        payload = jsonable(response) if response is not None else None
        record = {
            **pending,
            "ended_at": utc_now(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "response": payload,
            "error": (
                {"type": type(error).__name__, "message": str(error)}
                if error is not None
                else None
            ),
        }
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        return record


def install_openai_interaction_journal(
    path: Path,
    *,
    call_offset: int = 0,
    previous_prompt: list[Any] | None = None,
) -> InteractionJournal:
    """Wrap Verifiers' native OpenAI call and retain its request and response."""
    from verifiers.clients.openai_chat_completions_client import (
        OpenAIChatCompletionsClient,
    )

    journal = InteractionJournal(
        path,
        call_offset=call_offset,
        previous_prompt=previous_prompt,
    )
    current = OpenAIChatCompletionsClient.get_native_response
    original = getattr(current, "_mblab_original", current)

    async def get_native_response(
        self,
        prompt,
        model,
        sampling_args,
        tools=None,
        **kwargs,
    ):
        pending = journal.begin(
            prompt=prompt,
            model=model,
            sampling_args=sampling_args,
            tools=tools,
        )
        try:
            response = await original(
                self,
                prompt,
                model,
                sampling_args,
                tools,
                **kwargs,
            )
        except BaseException as error:
            journal.finish(pending, error=error)
            raise
        journal.finish(pending, response=response)
        return response

    get_native_response._mblab_original = original  # type: ignore[attr-defined]
    OpenAIChatCompletionsClient.get_native_response = get_native_response
    return journal
