"""Filesystem-backed run catalog for the local control center."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from mblab.metrics import derive_metrics


RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_jsonl(path: Path) -> list[Any]:
    """Read complete JSONL records, ignoring a partially-written final line."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Multiple HTTP workers can derive the same disposable artifact at once.
    # Give each atomic writer its own staging name so one replace cannot steal
    # another worker's temporary file.
    temporary = path.with_suffix(
        path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


ASCII_FENCE = re.compile(r"```text\r?\n(.*?)\r?\n```", re.DOTALL)
JSON_FENCE = re.compile(r"```json\r?\n(.*?)\r?\n```", re.DOTALL)


def interaction_model_inputs(records: list[dict[str, Any]]) -> dict[int, str]:
    """Return the exact fenced ASCII or JSON observation sent on each call."""
    inputs: dict[int, str] = {}
    for record in records:
        try:
            call = int(record.get("call"))
        except (TypeError, ValueError):
            continue
        messages = (record.get("request") or {}).get("appended_messages") or []
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            match = ASCII_FENCE.search(content) or JSON_FENCE.search(content)
            if match:
                inputs[call] = match.group(1)
                break
    return inputs


def derive_model_performance(
    interactions: list[dict[str, Any]],
    compactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate live provider usage and timing without double-counting forks."""

    totals = {
        key: 0.0
        for key in (
            "calls",
            "successful_calls",
            "failed_calls",
            "compaction_calls",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "latency_ms",
            "prompt_tokens_processed",
            "prompt_ms",
            "generated_tokens",
            "generation_ms",
        )
    }

    def numeric(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0

    def add_usage(usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        totals["input_tokens"] += numeric(
            usage.get("prompt_tokens") or usage.get("input_tokens")
        )
        totals["output_tokens"] += numeric(
            usage.get("completion_tokens") or usage.get("output_tokens")
        )
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            totals["cached_input_tokens"] += numeric(details.get("cached_tokens"))

    def add_timings(timings: Any) -> None:
        if not isinstance(timings, dict):
            return
        totals["prompt_tokens_processed"] += numeric(timings.get("prompt_n"))
        totals["prompt_ms"] += numeric(timings.get("prompt_ms"))
        totals["generated_tokens"] += numeric(timings.get("predicted_n"))
        totals["generation_ms"] += numeric(timings.get("predicted_ms"))

    for record in interactions:
        if not isinstance(record, dict) or record.get("inherited_from"):
            continue
        totals["calls"] += 1
        totals["failed_calls" if record.get("error") else "successful_calls"] += 1
        totals["latency_ms"] += numeric(record.get("latency_ms"))
        response = record.get("response") or {}
        if isinstance(response, dict):
            add_usage(response.get("usage"))
            add_timings(response.get("timings"))

    for record in compactions or []:
        if (
            not isinstance(record, dict)
            or record.get("inherited_from")
            or record.get("status") == "started"
        ):
            continue
        usages = [record.get("response_usage"), record.get("repair_usage")]
        timings = [record.get("response_timings"), record.get("repair_timings")]
        request_count = max(
            sum(bool(item) for item in usages),
            sum(bool(item) for item in timings),
        )
        if not request_count and record.get("status") in {
            "completed",
            "failed",
            "fallback",
        }:
            request_count = 1
        totals["calls"] += request_count
        totals["compaction_calls"] += request_count
        if record.get("status") == "failed":
            totals["failed_calls"] += 1
        else:
            totals["successful_calls"] += request_count
        totals["latency_ms"] += numeric(record.get("latency_ms"))
        for usage in usages:
            add_usage(usage)
        for timing in timings:
            add_timings(timing)

    input_tokens = int(totals["input_tokens"])
    output_tokens = int(totals["output_tokens"])
    calls = int(totals["calls"])
    prompt_ms = totals["prompt_ms"]
    generation_ms = totals["generation_ms"]
    return {
        "calls": calls,
        "successful_calls": int(totals["successful_calls"]),
        "failed_calls": int(totals["failed_calls"]),
        "compaction_calls": int(totals["compaction_calls"]),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_input_tokens": int(totals["cached_input_tokens"]),
        "cache_rate": round(totals["cached_input_tokens"] / input_tokens, 4)
        if input_tokens
        else None,
        "latency_ms": round(totals["latency_ms"], 3),
        "average_latency_ms": round(totals["latency_ms"] / calls, 3)
        if calls
        else None,
        "prompt_tokens_per_second": round(
            totals["prompt_tokens_processed"] * 1000 / prompt_ms, 3
        )
        if prompt_ms
        else None,
        "prompt_tokens_processed": int(totals["prompt_tokens_processed"]),
        "prompt_ms": round(prompt_ms, 3),
        "output_tokens_per_second": round(
            totals["generated_tokens"] * 1000 / generation_ms, 3
        )
        if generation_ms
        else None,
        "generated_tokens": int(totals["generated_tokens"]),
        "generation_ms": round(generation_ms, 3),
        "provider_compute_s": round((prompt_ms + generation_ms) / 1000, 3),
    }


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._performance_cache: dict[
            str, tuple[tuple[tuple[int, int], ...], dict[str, Any]]
        ] = {}

    def path_for(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run id")
        path = (self.root / run_id).resolve()
        if path.parent != self.root:
            raise ValueError("invalid run path")
        return path

    def list(self) -> list[dict[str, Any]]:
        runs = []
        for path in self.root.iterdir():
            if not path.is_dir() or not RUN_ID.fullmatch(path.name):
                continue
            run = self.load(path.name, include_timeline=False)
            if run:
                runs.append(run)
        return sorted(
            runs,
            key=lambda run: (str(run.get("created_at") or ""), run["id"]),
            reverse=True,
        )

    @staticmethod
    def _journal(path: Path, name: str) -> list[dict[str, Any]]:
        records = read_json(path / f"{name}.json", None)
        if not isinstance(records, list):
            records = read_jsonl(path / f"{name}.jsonl")
        return [record for record in records if isinstance(record, dict)]

    def _performance(self, run_id: str, path: Path) -> dict[str, Any]:
        candidates = [
            path / "interactions.json",
            path / "interactions.jsonl",
            path / "compactions.json",
            path / "compactions.jsonl",
        ]
        signature = tuple(
            (candidate.stat().st_mtime_ns, candidate.stat().st_size)
            if candidate.exists()
            else (0, 0)
            for candidate in candidates
        )
        cached = self._performance_cache.get(run_id)
        if cached and cached[0] == signature:
            return cached[1]
        performance = derive_model_performance(
            self._journal(path, "interactions"),
            self._journal(path, "compactions"),
        )
        self._performance_cache[run_id] = (signature, performance)
        return performance

    def load(self, run_id: str, *, include_timeline: bool = True) -> dict[str, Any] | None:
        path = self.path_for(run_id)
        if not path.is_dir():
            return None
        manifest = read_json(path / "run.json", {})
        summary = read_json(path / "summary.json", {})
        metadata = read_json(path / "metadata.json", {})
        actions = read_json(path / "actions.json", [])
        if not actions:
            actions = read_jsonl(path / "actions.jsonl")
        metrics = derive_metrics(actions, summary, metadata)
        interactions = self._journal(path, "interactions") if include_timeline else []
        compactions = self._journal(path, "compactions") if include_timeline else []
        performance = (
            derive_model_performance(interactions, compactions)
            if include_timeline
            else self._performance(run_id, path)
        )
        metrics["performance"] = performance
        if performance["total_tokens"] > metrics["summary"]["total_tokens"]:
            metric_summary = metrics["summary"]
            metric_summary["input_tokens"] = performance["input_tokens"]
            metric_summary["output_tokens"] = performance["output_tokens"]
            metric_summary["total_tokens"] = performance["total_tokens"]
            branch_actions = int(metric_summary.get("branch_actions") or 0)
            metric_summary["tokens_per_action"] = (
                round(performance["total_tokens"] / branch_actions, 1)
                if branch_actions
                else None
            )
        if include_timeline:
            model_inputs = interaction_model_inputs(
                [record for record in interactions if isinstance(record, dict)]
            )
            for row in metrics["timeline"]:
                row["model_board"] = model_inputs.get(int(row["turn"]), "")
        created_at = (
            manifest.get("created_at")
            or metadata.get("date")
            or (actions[0].get("timestamp") if actions else None)
        )
        status = manifest.get("status") or ("completed" if summary else "incomplete")
        replay_path = next(
            (
                candidate
                for candidate in (
                    path / "artifacts" / "maze_replay.mp4",
                    path / "maze_replay.mp4",
                )
                if candidate.is_file()
            ),
            None,
        )
        replay_stat = replay_path.stat() if replay_path else None
        replay_manifest = read_json(
            path / "artifacts" / ".maze_replay_manifest.json", None
        )
        artifacts = {
            "actions": (path / "actions.json").exists() or (path / "actions.jsonl").exists(),
            "interactions": (path / "interactions.json").exists()
            or (path / "interactions.jsonl").exists(),
            "compactions": (path / "compactions.json").exists()
            or (path / "compactions.jsonl").exists(),
            "rollout": (path / "rollout.jsonl").exists(),
            "replay_video": replay_path is not None,
            # A stable cache-buster that changes whenever an MP4 is replaced.
            "replay_version": (
                f"{replay_stat.st_mtime_ns:x}-{replay_stat.st_size:x}"
                if replay_stat
                else None
            ),
            "log": (path / "runner.log").exists(),
            "model_log": (path / "model-server.log").exists(),
            "model_service": (path / "model-service.json").exists(),
            "fork": (path / "fork.json").exists(),
        }
        result = {
            "id": run_id,
            "schema_version": manifest.get("schema_version", 0),
            "status": status,
            "created_at": created_at,
            "ended_at": manifest.get("ended_at"),
            "config": manifest.get("config")
            or {
                "model": metadata.get("model", "local"),
                "actions": summary.get("actions_budget"),
                "temperature": (summary.get("sampling_args") or {}).get("temperature"),
                "system_prompt": summary.get("system_prompt"),
                "base_url": metadata.get("base_url"),
            },
            "stop_condition": summary.get("stop_condition"),
            "benchmark": manifest.get("benchmark") or {},
            "lineage": (
                "fork"
                if isinstance((manifest.get("config") or {}).get("fork"), dict)
                else "experimental"
                if (manifest.get("config") or {}).get("system_prompt") == "unofficial"
                else "official"
                if manifest.get("schema_version", 0) >= 2
                and str((manifest.get("config") or {}).get("profile", "")).startswith("official-")
                else "legacy"
            ),
            "fork": (manifest.get("config") or {}).get("fork"),
            "error": manifest.get("error") or summary.get("error"),
            "artifacts": artifacts,
            "replay_manifest": replay_manifest
            if isinstance(replay_manifest, dict)
            else None,
            "metrics": metrics,
        }
        if not include_timeline:
            result["metrics"] = {**metrics, "timeline": []}
        return result

    def load_interactions(self, run_id: str) -> list[dict[str, Any]]:
        """Load completed or live model-call records for a run."""
        path = self.path_for(run_id)
        if not path.is_dir():
            raise FileNotFoundError(run_id)
        records = read_json(path / "interactions.json", None)
        if not isinstance(records, list):
            records = read_jsonl(path / "interactions.jsonl")
        return [record for record in records if isinstance(record, dict)]

    def load_compactions(self, run_id: str) -> list[dict[str, Any]]:
        """Load completed or live automatic-compaction audit events."""
        path = self.path_for(run_id)
        if not path.is_dir():
            raise FileNotFoundError(run_id)
        records = read_json(path / "compactions.json", None)
        if not isinstance(records, list):
            records = read_jsonl(path / "compactions.jsonl")
        return [record for record in records if isinstance(record, dict)]

    def replay_path(self, run_id: str) -> Path | None:
        path = self.path_for(run_id)
        for candidate in (path / "artifacts" / "maze_replay.mp4", path / "maze_replay.mp4"):
            if candidate.is_file():
                return candidate
        return None
