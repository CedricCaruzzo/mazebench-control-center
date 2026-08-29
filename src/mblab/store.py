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


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

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
        if include_timeline:
            interactions = read_json(path / "interactions.json", None)
            if not isinstance(interactions, list):
                interactions = read_jsonl(path / "interactions.jsonl")
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
