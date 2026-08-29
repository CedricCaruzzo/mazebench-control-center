"""Auditable generic context compaction for long-running local agents.

The official legacy MazeBench adapter constructs an append-only transcript.
This module optionally bounds the model-visible working context at the call
boundary without changing the official environment, prompt, observations, or
action parser. The policy is deliberately domain-neutral and never receives
decoded MazeBench state, renderer state, or evaluator-private data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx


MODE = "generic-autocompact"
NO_COMPACTION_MODE = "none"
DEFAULT_COMPACT_AT = 60_000
DEFAULT_SOURCE_COMPACT_AT = 32_000
DEFAULT_TARGET_TOKENS = 2_048
DEFAULT_RECENT_TURNS = 4
MIN_SUMMARY_TIMEOUT_SECONDS = 900

COMPACTION_SYSTEM_PROMPT = """You compact long-running agent conversations.
Create a concise continuation summary of the supplied earlier conversation.
Preserve relevant discoveries, attempted approaches, observed results, failures,
uncertainties, commitments, and unfinished work. Preserve the agent's own
hypotheses as hypotheses, not facts. Do not add information, solve the task, or
introduce domain-specific structure that was not already present. The summary
will replace the supplied history, so write only the factual continuation
summary and no preamble."""

SUMMARY_SUMMARY_REPAIR_SYSTEM_PROMPT = """Repair a truncated or malformed continuation
summary. Preserve supported content, remove repetition, and return only a
complete concise replacement without a preamble."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_chat_body(
    messages: list[dict[str, Any]],
    model: str,
    sampling_args: dict[str, Any],
    tools: list[Any] | None,
) -> dict[str, Any]:
    """Render the same request shape Verifiers sends to llama.cpp."""
    request_args = dict(jsonable(sampling_args) or {})
    if "max_tokens" in request_args:
        request_args["max_completion_tokens"] = request_args.pop("max_tokens")
    extra_body = request_args.pop("extra_body", {}) or {}
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        **request_args,
        **extra_body,
    }
    if tools:
        body["tools"] = jsonable(tools)
    return body


def estimated_chat_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative provider-neutral fallback when no tokenizer API exists.

    This estimate is intentionally labeled in every run and should use a lower
    compaction threshold than an exact provider counter. UTF-8 bytes are used so
    Unicode observations cannot be drastically undercounted.
    """
    serialized = json.dumps(messages, ensure_ascii=False, default=str).encode()
    return max(1, math.ceil(len(serialized) / 3) + len(messages) * 6)


def compacted_context_message(summary: str, compaction_index: int) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Automatic context compaction checkpoint "
            f"{compaction_index}. Continue the same task, episode, and state; "
            "this summary replaces older history, not a reset.\n\n"
            "<conversation-summary>\n"
            f"{summary.strip()}\n"
            "</conversation-summary>"
        ),
    }


TokenCounter = Callable[
    [list[dict[str, Any]], str, dict[str, Any], list[Any] | None],
    Awaitable[int],
]
SummaryGenerator = Callable[
    [list[dict[str, Any]], str, int, int],
    Awaitable[tuple[str, dict[str, Any]]],
]
RepairGenerator = Callable[
    [str, list[dict[str, Any]], str, int, int],
    Awaitable[tuple[str, dict[str, Any]]],
]


def summary_timeout_seconds(source_tokens: int, summary_budget_tokens: int) -> int:
    """Allow for uncached prefill and deliberately conservative generation.

    Compaction prompts do not share the main trajectory's KV-cache prefix. The
    estimate assumes only 50 input tokens/s, 4 output tokens/s, and adds three
    minutes of transport/queue headroom. The 15-minute floor also keeps small
    summaries from inheriting an ordinary chat-request deadline.
    """
    estimated = math.ceil(
        max(source_tokens, 0) / 50
        + max(summary_budget_tokens, 0) / 4
        + 180
    )
    return max(MIN_SUMMARY_TIMEOUT_SECONDS, estimated)


class CompactionJournal:
    """Append complete compaction events for audit and later ablation work."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(jsonable(event), separators=(",", ":"), default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())


class GenericAutoCompactor:
    """Maintain a bounded working prompt over an immutable raw trajectory."""

    mode = MODE
    artifact_kind = "continuation_summary"
    nonfatal_generation = False
    retry_delay_calls = 4

    def __init__(
        self,
        *,
        base_url: str,
        journal_path: Path,
        api_key_env: str = "MAZEBENCH_API_KEY",
        token_count_mode: str = "llama.cpp",
        thinking_contract: str = "qwen",
        compact_at_tokens: int = DEFAULT_COMPACT_AT,
        source_compact_at_tokens: int = DEFAULT_SOURCE_COMPACT_AT,
        summary_budget_tokens: int = DEFAULT_TARGET_TOKENS,
        recent_turns: int = DEFAULT_RECENT_TURNS,
        call_offset: int = 0,
        inherited_assistant_count: int = 0,
        token_counter: TokenCounter | None = None,
        summary_generator: SummaryGenerator | None = None,
        repair_generator: RepairGenerator | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.token_count_mode = token_count_mode
        self.thinking_contract = thinking_contract
        if token_count_mode not in {"llama.cpp", "estimate"}:
            raise ValueError("token_count_mode must be llama.cpp or estimate")
        if thinking_contract not in {"qwen", "none"}:
            raise ValueError("thinking_contract must be qwen or none")
        self.compact_at_tokens = int(compact_at_tokens)
        self.source_compact_at_tokens = int(source_compact_at_tokens)
        self.summary_budget_tokens = int(summary_budget_tokens)
        self.recent_turns = int(recent_turns)
        self.call_offset = max(0, int(call_offset))
        self.inherited_assistant_count = max(0, int(inherited_assistant_count))
        self.journal = CompactionJournal(journal_path)
        self._token_counter = token_counter or self._count_tokens_http
        self._summary_generator = summary_generator or self._generate_summary_http
        self._repair_generator = (
            repair_generator
            if repair_generator is not None
            else (self._repair_summary_http if summary_generator is None else None)
        )
        self._summary: str | None = None
        # Raw message index immediately after the history represented by the
        # summary. Index zero is the immutable official system message.
        self._compacted_through = 1
        self._compaction_index = 0
        self._attempt_index = 0
        self._immutable_system_message: dict[str, Any] | None = None
        self._retry_after_call = 0

    @property
    def compaction_count(self) -> int:
        return self._compaction_index

    def _context_message(self, summary: str, index: int) -> dict[str, str]:
        return compacted_context_message(summary, index)

    def _active_prompt(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._summary:
            return raw
        return [
            raw[0],
            self._context_message(self._summary, self._compaction_index),
            *raw[self._compacted_through :],
        ]

    def _desired_cut(self, raw: list[dict[str, Any]]) -> int | None:
        # At a decision boundary the legacy transcript is:
        #   system, initial user, assistant, user, ..., assistant, current user
        # Keeping 2*N messages retains N complete action/result cycles (the
        # latest user message is the current observation) and starts the tail
        # with an assistant turn.
        keep_messages = self.recent_turns * 2
        cut = len(raw) - keep_messages
        if cut > 1:
            cut -= cut % 2  # assistant messages occupy even raw indices
        if cut <= self._compacted_through:
            return None
        return cut

    def _source_messages(
        self, raw: list[dict[str, Any]], cut: int
    ) -> list[dict[str, Any]]:
        source: list[dict[str, Any]] = []
        if self._summary:
            source.append(self._context_message(self._summary, self._compaction_index))
        source.extend(raw[self._compacted_through : cut])
        return source

    def _source_prompt(self, source: list[dict[str, Any]]) -> list[dict[str, str]]:
        # Keeping the original fields (including local-model reasoning) in the
        # serialized transcript lets generic compaction preserve the agent's
        # own work without giving it any evaluator-private information.
        transcript = json.dumps(source, ensure_ascii=False, default=str)
        return [
            {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "<conversation-to-compact>\n"
                + transcript
                + "\n</conversation-to-compact>",
            },
        ]

    def _validate_summary(self, summary: str) -> None:
        if not str(summary).strip():
            raise RuntimeError("automatic compaction returned an empty summary")

    def _event_metadata(self) -> dict[str, Any]:
        return {}

    def _before_generation(
        self,
        raw: list[dict[str, Any]],
        cut: int,
    ) -> None:
        del raw, cut

    def _before_source_prompt(
        self,
        raw: list[dict[str, Any]],
        cut: int,
    ) -> None:
        del raw, cut

    def _trigger_override(
        self,
        raw: list[dict[str, Any]],
        cut: int,
        before_call: int,
    ) -> str | None:
        del raw, cut, before_call
        return None

    def _augment_active_prompt(
        self,
        active: list[dict[str, Any]],
        raw: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        del raw
        return active

    def _fallback_summary(self, error: BaseException) -> str | None:
        del error
        return None

    def _after_compaction_success(self) -> None:
        return None

    def _repair_prompt(
        self,
        partial: str,
        source: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        required = getattr(self, "required_sections", ())
        recent = json.dumps(source[-4:], ensure_ascii=False, default=str)
        return [
            {"role": "system", "content": SUMMARY_REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Required headings in order:\n"
                    + "\n".join(f"## {section}" for section in required)
                    + "\n\n<partial-memory>\n"
                    + partial
                    + "\n</partial-memory>\n\n<recent-visible-evidence>\n"
                    + recent
                    + "\n</recent-visible-evidence>"
                ),
            },
        ]

    async def _count_tokens_http(
        self,
        messages: list[dict[str, Any]],
        model: str,
        sampling_args: dict[str, Any],
        tools: list[Any] | None,
    ) -> int:
        if self.token_count_mode == "estimate":
            return estimated_chat_tokens(messages)
        body = normalize_chat_body(messages, model, sampling_args, tools)
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions/input_tokens",
                headers=self._auth_headers(),
                json=body,
            )
            response.raise_for_status()
            return int(response.json()["input_tokens"])

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {os.environ.get(self.api_key_env) or 'none'}"
        }

    def _compaction_extensions(self) -> dict[str, Any]:
        if self.thinking_contract != "qwen":
            return {}
        return {
            "chat_template_kwargs": {
                "enable_thinking": False,
                "preserve_thinking": False,
            },
            "thinking_budget_tokens": 0,
        }

    async def _generate_summary_http(
        self,
        source: list[dict[str, Any]],
        model: str,
        budget: int,
        timeout_seconds: int,
    ) -> tuple[str, dict[str, Any]]:
        messages = self._source_prompt(source)
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": budget,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            **self._compaction_extensions(),
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10)
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._auth_headers(),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        message = ((payload.get("choices") or [{}])[0].get("message") or {})
        summary = message.get("content") or message.get("reasoning_content") or ""
        return str(summary).strip(), payload

    async def _repair_summary_http(
        self,
        partial: str,
        source: list[dict[str, Any]],
        model: str,
        budget: int,
        timeout_seconds: int,
    ) -> tuple[str, dict[str, Any]]:
        body = {
            "model": model,
            "messages": self._repair_prompt(partial, source),
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": budget,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            **self._compaction_extensions(),
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10)
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._auth_headers(),
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        message = ((payload.get("choices") or [{}])[0].get("message") or {})
        repaired = message.get("content") or message.get("reasoning_content") or ""
        return str(repaired).strip(), payload

    async def _source_token_count(
        self, source: list[dict[str, Any]], model: str
    ) -> int:
        prompt = self._source_prompt(source)
        args: dict[str, Any] = {
            "temperature": 0.0,
            "max_tokens": self.summary_budget_tokens,
        }
        if self.thinking_contract == "qwen":
            args["extra_body"] = {
                "chat_template_kwargs": {
                    "enable_thinking": False,
                    "preserve_thinking": False,
                },
                "thinking_budget_tokens": 0,
            }
        return await self._token_counter(prompt, model, args, None)

    async def prepare(
        self,
        prompt: list[Any],
        model: str,
        sampling_args: dict[str, Any],
        tools: list[Any] | None,
    ) -> list[dict[str, Any]]:
        raw_value = jsonable(prompt)
        raw = [message for message in raw_value if isinstance(message, dict)]
        if not raw or raw[0].get("role") != "system":
            raise RuntimeError("automatic compaction requires the official system message")
        self._immutable_system_message = dict(raw[0])
        if not str(self._immutable_system_message.get("content") or "").strip():
            raise RuntimeError("automatic compaction requires non-empty system instructions")
        if self._compacted_through > len(raw):
            raise RuntimeError("raw conversation became shorter after automatic compaction")
        # The immutable raw transcript contains one assistant message for each
        # completed model decision, including inherited fork history. This
        # makes compaction placement explicit without coupling the compactor to
        # the interaction-journal wrapper.
        assistant_count = sum(
            1 for message in raw if message.get("role") == "assistant"
        )
        before_call = (
            self.call_offset
            + assistant_count
            - self.inherited_assistant_count
            + 1
        )

        # Imported or explicitly raw-history forks can begin with hundreds of
        # historical turns. Active-context forks normally start from the
        # parent's latest bounded prompt instead.
        # Each successful pass advances `_compacted_through`, so one pass per
        # prior assistant turn is a finite conservative ceiling. Ordinary runs
        # still compact once; large forks may require several hierarchical
        # summaries before their first new action.
        max_passes = max(
            4,
            sum(1 for message in raw if message.get("role") == "assistant"),
        )
        for _ in range(max_passes):
            active = self._active_prompt(raw)
            active_tokens = await self._token_counter(
                active, model, sampling_args, tools
            )
            cut = self._desired_cut(raw)
            if cut is None:
                if active_tokens >= self.compact_at_tokens:
                    raise RuntimeError(
                        "automatic compaction cannot reduce the recent "
                        f"verbatim tail ({active_tokens} tokens)"
                    )
                return self._augment_active_prompt(active, raw)

            source = self._source_messages(raw, cut)
            self._before_source_prompt(raw, cut)
            # Avoid tokenizing a second long prompt on the early turns. Three
            # characters/token is conservative for the mixed ASCII/reasoning
            # transcript and only controls when exact counting begins.
            source_estimate = len(
                json.dumps(source, ensure_ascii=False, default=str)
            ) // 3
            source_tokens = None
            if source_estimate >= int(self.source_compact_at_tokens * 0.70):
                source_tokens = await self._source_token_count(source, model)

            trigger = None
            if active_tokens >= self.compact_at_tokens:
                trigger = "working_context"
            elif (
                source_tokens is not None
                and source_tokens >= self.source_compact_at_tokens
            ):
                trigger = "compaction_source"
            override = self._trigger_override(raw, cut, before_call)
            if trigger is None and override and before_call >= self._retry_after_call:
                trigger = override
            if trigger is None:
                return self._augment_active_prompt(active, raw)
            if (
                before_call < self._retry_after_call
                and trigger != "working_context"
            ):
                return self._augment_active_prompt(active, raw)

            if source_tokens is None:
                source_tokens = await self._source_token_count(source, model)
            # A single very large reasoning turn can jump past the proactive
            # source threshold. Compact the oldest safe chunk first; the loop
            # will immediately compact another chunk if the remaining working
            # prompt is still too large.
            while (
                source_tokens > self.compact_at_tokens
                and cut > self._compacted_through + 2
            ):
                span = cut - self._compacted_through
                safe_span = max(
                    2,
                    int(
                        span
                        * (self.compact_at_tokens / max(source_tokens, 1))
                        * 0.85
                    ),
                )
                cut = self._compacted_through + safe_span
                cut -= cut % 2
                if cut <= self._compacted_through:
                    cut = self._compacted_through + 1
                    if cut % 2:
                        cut += 1
                source = self._source_messages(raw, cut)
                self._before_source_prompt(raw, cut)
                source_tokens = await self._source_token_count(source, model)

            self._before_generation(raw, cut)
            started_at = utc_now()
            started = time.perf_counter()
            before_through = self._compacted_through
            attempt = self._compaction_index + 1
            self._attempt_index += 1
            timeout_seconds = summary_timeout_seconds(
                source_tokens, self.summary_budget_tokens
            )
            common_event = {
                "schema_version": 1,
                "mode": self.mode,
                "artifact_kind": self.artifact_kind,
                **self._event_metadata(),
                "compaction": attempt,
                "attempt": self._attempt_index,
                "before_call": before_call,
                "trigger": trigger,
                "model": model,
                "token_count_mode": self.token_count_mode,
                "thinking_contract": self.thinking_contract,
                "raw_message_range": [before_through, cut],
                "raw_messages_compacted": cut - before_through,
                "recent_turns_retained": self.recent_turns,
                "working_tokens_before": active_tokens,
                "source_tokens": source_tokens,
                "summary_budget_tokens": self.summary_budget_tokens,
                "timeout_seconds": timeout_seconds,
                "source_sha256": digest(source),
            }
            # Persist the exact model-visible source before making the slow
            # request. A timeout, process exit, or manual stop must not leave an
            # empty audit journal as if compaction had never started.
            self.journal.append(
                {
                    **common_event,
                    "status": "started",
                    "started_at": started_at,
                    "source_messages": source,
                }
            )
            summary = ""
            response: dict[str, Any] = {}
            repair_summary = ""
            repair_response: dict[str, Any] = {}
            repair_error: BaseException | None = None
            generation_error: BaseException | None = None
            initial_generation_error: BaseException | None = None
            rejected_summary = ""
            try:
                summary, response = await self._summary_generator(
                    source,
                    model,
                    self.summary_budget_tokens,
                    timeout_seconds,
                )
                self._validate_summary(summary)
            except BaseException as error:
                generation_error = error
                initial_generation_error = error
                rejected_summary = summary
                if (
                    isinstance(error, Exception)
                    and summary
                    and self._repair_generator is not None
                ):
                    try:
                        repair_summary, repair_response = await self._repair_generator(
                            summary,
                            source,
                            model,
                            self.summary_budget_tokens,
                            timeout_seconds,
                        )
                        self._validate_summary(repair_summary)
                        summary = repair_summary
                        generation_error = None
                    except BaseException as candidate_error:
                        repair_error = candidate_error

            if generation_error is not None:
                error = generation_error
                fallback = (
                    self._fallback_summary(error)
                    if self.nonfatal_generation and isinstance(error, Exception)
                    else None
                )
                if fallback:
                    self._validate_summary(fallback)
                    summary = fallback
                else:
                    status = (
                        "fallback"
                        if self.nonfatal_generation and isinstance(error, Exception)
                        else "failed"
                    )
                    self._retry_after_call = before_call + self.retry_delay_calls
                    self.journal.append(
                        {
                            **common_event,
                            "status": status,
                            "started_at": started_at,
                            "ended_at": utc_now(),
                            "latency_ms": round(
                                (time.perf_counter() - started) * 1000, 3
                            ),
                            "partial_summary": rejected_summary or None,
                            "partial_summary_sha256": (
                                digest(rejected_summary) if rejected_summary else None
                            ),
                            "response_finish_reason": (
                                ((response.get("choices") or [{}])[0]).get(
                                    "finish_reason"
                                )
                                if response
                                else None
                            ),
                            "response_usage": jsonable(response.get("usage") or {}),
                            "response_timings": jsonable(
                                response.get("timings") or {}
                            ),
                            "repair_attempted": bool(repair_summary or repair_error),
                            "repair_partial_summary": repair_summary or None,
                            "repair_error": (
                                {
                                    "type": type(repair_error).__name__,
                                    "message": str(repair_error) or repr(repair_error),
                                }
                                if repair_error is not None
                                else None
                            ),
                            "fallback_checkpoint": self._compaction_index or None,
                            "retry_after_call": self._retry_after_call,
                            "error": {
                                "type": type(error).__name__,
                                "message": str(error) or repr(error),
                                "repr": repr(error),
                            },
                        }
                    )
                    if status == "failed":
                        raise error
                    return self._augment_active_prompt(active, raw)

            if generation_error is not None and summary:
                recovery = "deterministic_fallback"
            elif repair_summary:
                recovery = "model_repair"
            else:
                recovery = None
            self._compaction_index += 1
            self._summary = summary
            self._compacted_through = cut
            self._after_compaction_success()
            after = self._active_prompt(raw)
            after_tokens = await self._token_counter(
                after, model, sampling_args, tools
            )
            event = {
                **common_event,
                "status": "completed",
                "started_at": started_at,
                "ended_at": utc_now(),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "working_tokens_after": after_tokens,
                "summary": summary,
                "summary_sha256": digest(summary),
                "response_usage": jsonable(response.get("usage") or {}),
                "response_timings": jsonable(response.get("timings") or {}),
                "response_finish_reason": (
                    ((response.get("choices") or [{}])[0]).get("finish_reason")
                    if response
                    else None
                ),
                "recovery": recovery,
                "partial_summary": rejected_summary or None,
                "repair_usage": jsonable(repair_response.get("usage") or {}),
                "repair_timings": jsonable(repair_response.get("timings") or {}),
                "recovery_error": (
                    {
                        "type": type(initial_generation_error).__name__,
                        "message": str(initial_generation_error)
                        or repr(initial_generation_error),
                    }
                    if recovery and initial_generation_error is not None
                    else None
                ),
                "repair_error": (
                    {
                        "type": type(repair_error).__name__,
                        "message": str(repair_error) or repr(repair_error),
                    }
                    if repair_error is not None
                    else None
                ),
            }
            self.journal.append(event)
            self._retry_after_call = 0
            if after_tokens < self.compact_at_tokens:
                return self._augment_active_prompt(after, raw)

        raise RuntimeError(
            "automatic compaction exhausted its finite progress bound "
            f"after {max_passes} passes"
        )


def install_generic_autocompaction(
    compactor: GenericAutoCompactor,
) -> GenericAutoCompactor:
    """Wrap Verifiers so only the bounded prompt reaches the normal journal."""
    from verifiers.clients.openai_chat_completions_client import (
        OpenAIChatCompletionsClient,
    )

    current = OpenAIChatCompletionsClient.get_native_response

    async def get_native_response(
        self,
        prompt,
        model,
        sampling_args,
        tools=None,
        **kwargs,
    ):
        bounded_prompt = await compactor.prepare(
            prompt, model, sampling_args, tools
        )
        return await current(
            self,
            bounded_prompt,
            model,
            sampling_args,
            tools,
            **kwargs,
        )

    get_native_response._mblab_compactor = compactor  # type: ignore[attr-defined]
    OpenAIChatCompletionsClient.get_native_response = get_native_response
    return compactor
