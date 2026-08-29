"""Create privacy-conscious, portable run bundles for publication."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from mblab.store import RUN_ID


HOME_PATH = re.compile(r"(?:(?:/Users|/home)/[^/\s\"']+|[A-Za-z]:\\Users\\[^\\\s\"']+)")
SAFE_ROOT_FILES = {
    "actions.json",
    "actions.jsonl",
    "benchmark-contract.json",
    "completion.json",
    "compactions.json",
    "compactions.jsonl",
    "fork.json",
    "interactions.json",
    "interactions.jsonl",
    "metadata.json",
    "metrics.json",
    "model-service.json",
    "rollout.jsonl",
    "run.json",
    "summary.json",
    "system-prompt.txt",
}
LOG_FILES = {"runner.log", "model-server.log", "artifacts/replay.log"}
REPLAY_FILES = {
    "artifacts/maze_replay.mp4",
    "artifacts/maze_scorecard.json",
    "artifacts/maze_actions.txt",
    "artifacts/.maze_replay_manifest.json",
    "artifacts/viewer-snapshots.json",
}


def _redact_string(value: str) -> str:
    return HOME_PATH.sub("<local-home>", value)


def _sanitize(
    value: Any,
    *,
    include_reasoning: bool,
    redact_custom_prompts: bool,
) -> Any:
    if isinstance(value, list):
        return [
            _sanitize(
                item,
                include_reasoning=include_reasoning,
                redact_custom_prompts=redact_custom_prompts,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return _redact_string(value) if isinstance(value, str) else value
    result: dict[str, Any] = {}
    role = str(value.get("role") or "")
    for key, item in value.items():
        if key == "reasoning_content" and not include_reasoning:
            continue
        if redact_custom_prompts and key == "content" and role == "system":
            result[key] = "<redacted-unofficial-system-prompt>"
            continue
        result[str(key)] = _sanitize(
            item,
            include_reasoning=include_reasoning,
            redact_custom_prompts=redact_custom_prompts,
        )
    return result


def _json_bytes(
    path: Path,
    *,
    include_reasoning: bool,
    redact_custom_prompts: bool,
) -> bytes:
    if path.suffix == ".jsonl":
        records = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL in {path.name} line {line_number}: {error}") from None
        rendered = "\n".join(
            json.dumps(
                _sanitize(
                    record,
                    include_reasoning=include_reasoning,
                    redact_custom_prompts=redact_custom_prompts,
                ),
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            for record in records
        )
        return (rendered + ("\n" if rendered else "")).encode()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path.name}: {error}") from None
    sanitized = _sanitize(
        value,
        include_reasoning=include_reasoning,
        redact_custom_prompts=redact_custom_prompts,
    )
    return (json.dumps(sanitized, indent=2, ensure_ascii=False, default=str) + "\n").encode()


def export_run(
    run_dir: Path,
    output: Path,
    *,
    include_reasoning: bool = False,
    include_logs: bool = False,
    include_replay: bool = False,
    include_unofficial_prompt: bool = False,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not run_dir.is_dir() or not RUN_ID.fullmatch(run_dir.name):
        raise ValueError("run directory must exist and have a valid run ID")
    manifest_path = run_dir / "run.json"
    try:
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        raise ValueError("run directory has no valid run.json") from None
    unofficial = (run_manifest.get("config") or {}).get("system_prompt") == "unofficial"
    redact_custom_prompts = unofficial and not include_unofficial_prompt

    selected: list[tuple[str, Path]] = []
    for relative in sorted(SAFE_ROOT_FILES):
        path = run_dir / relative
        if path.is_file() and not path.is_symlink():
            if relative == "system-prompt.txt" and redact_custom_prompts:
                continue
            selected.append((relative, path))
    if include_logs:
        for relative in sorted(LOG_FILES):
            path = run_dir / relative
            if path.is_file() and not path.is_symlink():
                selected.append((relative, path))
    if include_replay:
        for relative in sorted(REPLAY_FILES):
            path = run_dir / relative
            if path.is_file() and not path.is_symlink():
                selected.append((relative, path))

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    files: list[dict[str, Any]] = []
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, path in selected:
            if path.suffix in {".json", ".jsonl"}:
                payload = _json_bytes(
                    path,
                    include_reasoning=include_reasoning,
                    redact_custom_prompts=redact_custom_prompts,
                )
            elif path.suffix in {".txt", ".log"}:
                payload = _redact_string(path.read_text(errors="replace")).encode()
            else:
                payload = path.read_bytes()
            archive_name = f"{run_dir.name}/{relative}"
            archive.writestr(archive_name, payload)
            files.append(
                {
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        export_manifest = {
            "schema_version": 1,
            "run_id": run_dir.name,
            "privacy": {
                "home_paths_redacted": True,
                "reasoning_included": include_reasoning,
                "logs_included": include_logs,
                "replay_included": include_replay,
                "unofficial_prompt_included": include_unofficial_prompt,
                "unofficial_prompt_redacted": redact_custom_prompts,
            },
            "files": files,
        }
        archive.writestr(
            f"{run_dir.name}/public-export.json",
            json.dumps(export_manifest, indent=2) + "\n",
        )
    temporary.replace(output)
    return {**export_manifest, "output": str(output)}
