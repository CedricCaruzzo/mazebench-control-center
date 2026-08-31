#!/usr/bin/env python3
"""Local HTTP control center for MazeBench experiments.

The server has no third-party web dependency. It serves a small SPA, launches
the existing baseline runner as a child process, and indexes immutable run
directories under ``runs/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mblab.official import (
    activate_official_environment,
    benchmark_contract,
    delivered_system_prompt,
    official_runtime_root,
    provenance,
)
from mblab.forks import load_fork_plan
from mblab.config import (
    default_model_profiles,
    load_model_profiles,
    public_model_profile,
)
from mblab.providers import OpenAICompatibleService, terminate_process
from mblab.store import RUN_ID, RunStore, read_json, write_json
from mblab.worlds import MazeBenchWorldService

activate_official_environment()

from mazebench.mazebench import MazeSession, parse_text_action  # noqa: E402


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def installed_resource(kind: str, name: str | None = None) -> Path:
    development = SOURCE_ROOT / kind
    installed = Path(sys.prefix) / "share" / "mazebench-control-center" / kind
    root = development if development.exists() else installed
    return root / name if name else root


WEB_ROOT = installed_resource("web")

# Public web files are intentionally explicit. Request paths never become
# filesystem paths, which keeps the local server's boundary small even when it
# is exposed through SSH forwarding or another reverse tunnel.
WEB_ASSETS: dict[str, str] = {
    "/app.js": "app.js",
    "/favicon.svg": "favicon.svg",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/viewer.css": "viewer.css",
    "/viewer.html": "viewer.html",
    "/viewer.js": "viewer.js",
}

CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".gif": "image/gif",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def web_asset_for(request_path: str) -> Path | None:
    """Map a URL path to a bundled asset without using it as a file path."""
    decoded = urllib.parse.unquote(request_path)
    name = "index.html" if decoded in {"", "/"} else WEB_ASSETS.get(decoded)
    if not name:
        return None
    root = WEB_ROOT.resolve()
    candidate = (root / name).resolve()
    return candidate if candidate.parent == root else None


def content_type_for(path: Path) -> str:
    """Return a fixed HTTP media type; never reflect filename text in a header."""
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


MODEL_PROFILES = default_model_profiles(SOURCE_ROOT)

CONTEXT_MODES: dict[str, dict[str, Any]] = {
    "generic-autocompact": {
        "id": "generic-autocompact",
        "label": "Generic automatic compaction",
        "description": "Domain-neutral model-generated summaries with a recent verbatim tail.",
    },
    "none": {
        "id": "none",
        "label": "Endpoint-managed / no Control Center compaction",
        "description": (
            "Forward the normal growing conversation unchanged; any context "
            "management belongs to the selected endpoint or upstream harness."
        ),
    },
}

OBSERVATION_MODES: dict[str, dict[str, str]] = {
    "ascii": {
        "id": "ascii",
        "label": "ASCII · perspective text",
        "description": (
            "Official perspective ASCII track; each tile is rendered as glyphs."
        ),
    },
    "json": {
        "id": "json",
        "label": "JSON · structured coordinates",
        "description": (
            "Official structured text track with visible objects and coordinates."
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"process", "model_process", "thread"}
    }


class ControlState:
    def __init__(
        self,
        repo_root: Path,
        runs_root: Path,
        *,
        model_profiles: dict[str, dict[str, Any]] | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.model_profiles = model_profiles or default_model_profiles(self.repo_root)
        self.store = RunStore(runs_root)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.viewer_lock = threading.Lock()
        runtime = official_runtime_root()
        self.replay_script = runtime / "scripts" / "maze-export-replay.js"
        self.replay_runtime = runtime
        self.official = provenance()
        self.official_benchmark_contract = benchmark_contract(hide_names=True)
        self.world_service = MazeBenchWorldService(
            official_root=runtime,
            repo_root=self.repo_root,
            runs_root=self.store.root,
            level_state_script=installed_resource("scripts", "official-level-state.js"),
        )
        self.level_state_cache: dict[str, dict[str, Any]] = {}
        world_map = read_json(runtime / "games" / "maze" / "world_map.json", {})
        self.world_rooms = sorted(
            f"level_{coordinates[0]}x{coordinates[1]}"
            for coordinates in (world_map.get("levels") or {}).values()
            if isinstance(coordinates, list) and len(coordinates) == 2
        )

    def level_state(self, level_id: str) -> dict[str, Any]:
        """Parse a level through the official server, cached by room id."""
        if level_id not in self.world_rooms:
            raise FileNotFoundError(level_id)
        with self.lock:
            cached = self.level_state_cache.get(level_id)
        if cached is not None:
            return cached
        completed = subprocess.run(
            [
                shutil.which("node") or "node",
                str(installed_resource("scripts", "official-level-state.js")),
                str(self.replay_runtime),
                level_id,
            ],
            cwd=self.replay_runtime,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "official level parser failed")
        value = json.loads(completed.stdout)
        with self.lock:
            self.level_state_cache[level_id] = value
        return value

    def _viewer_start_level(self, run_id: str, actions: list[dict[str, Any]]) -> str:
        run_dir = self.store.path_for(run_id)
        try:
            first_line = (run_dir / "rollout.jsonl").read_text().splitlines()[0]
            rollout = json.loads(first_line)
        except (FileNotFoundError, IndexError, json.JSONDecodeError):
            rollout = {}
        replay = (rollout or {}).get("maze_replay") or {}
        manifest = read_json(run_dir / "run.json", {})
        configured = (manifest.get("config") or {}).get("level")
        start = replay.get("start_level_id") or configured or "level_HxI"
        if not str(start).startswith("level_"):
            start = f"level_{start}"
        if start not in self.world_rooms and actions:
            start = ((actions[0].get("status") or {}).get("current_room") or start)
        return str(start)

    def viewer_snapshots(self, run_id: str) -> dict[str, Any]:
        """Reconstruct evaluator-private render states in an isolated engine."""
        run_dir = self.store.path_for(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(run_id)
        actions = read_json(run_dir / "actions.json", [])
        if not actions:
            from mblab.store import read_jsonl

            actions = read_jsonl(run_dir / "actions.jsonl")
        manifest = read_json(run_dir / "run.json", {})
        config = manifest.get("config") or {}
        signature_payload = [
            {
                "command": action.get("command"),
                "error": action.get("error"),
                "valid": action.get("valid"),
            }
            for action in actions
        ]
        signature = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True).encode()
        ).hexdigest()
        cache_path = run_dir / "artifacts" / "viewer-snapshots.json"
        cache = read_json(cache_path, {})
        if (
            cache.get("action_signature") == signature
            and cache.get("mazebench_version") == self.official["version"]
            and len(cache.get("snapshots") or []) == len(actions) + 1
        ):
            return cache

        session = MazeSession(
            game_won_gem_count=100,
            level_id=self._viewer_start_level(run_id, actions),
            observation_mode="ascii",
            omniscient=False,
            hide_names=bool(config.get("hide_names", False)),
            hide_names_seed=str(config.get("hide_names_seed") or "1"),
            node_bin=shutil.which("node") or "node",
            repo_root=str(self.replay_runtime),
            timeout_seconds=30,
            view=str(config.get("view") or "top-diagonal"),
            yaw=int(config.get("yaw") or 0),
        )
        snapshots: list[dict[str, Any]] = []
        try:
            status = session.request("observe")
            snapshots.append(status.get("_render_state") or {})
            for action in actions:
                if action.get("valid") is not False and action.get("command"):
                    try:
                        command, action_args = parse_text_action(str(action["command"]))
                        status = session.request(command, **action_args)
                    except Exception:
                        status = session.request("observe")
                else:
                    status = session.request("observe")
                snapshots.append(status.get("_render_state") or {})
        finally:
            session.close()
        result = {
            "schema_version": 1,
            "mazebench_version": self.official["version"],
            "action_signature": signature,
            "action_count": len(actions),
            "snapshots": snapshots,
        }
        write_json(cache_path, result)
        return result

    def viewer_snapshot(self, run_id: str, decision: int) -> dict[str, Any]:
        run_dir = self.store.path_for(run_id)
        actions = read_json(run_dir / "actions.json", [])
        if not actions:
            from mblab.store import read_jsonl

            actions = read_jsonl(run_dir / "actions.jsonl")
        total = len(actions)
        state_index = max(0, min(total, int(decision) - 1))
        # Schema-2 runs journal the official private render checkpoint after
        # each action. Decision N can therefore use action N-1 immediately,
        # avoiding a full replay every time the live dashboard refreshes.
        if state_index > 0:
            direct = actions[state_index - 1].get("render_state")
            if isinstance(direct, dict) and direct.get("level_id"):
                return {
                    "decision": max(1, int(decision)),
                    "state_index": state_index,
                    "action_count": total,
                    "mazebench_version": self.official["version"],
                    "render_state": direct,
                }
        with self.viewer_lock:
            data = self.viewer_snapshots(run_id)
        total = int(data.get("action_count") or 0)
        state_index = max(0, min(total, int(decision) - 1))
        snapshots = data.get("snapshots") or []
        if not snapshots or not isinstance(snapshots[state_index], dict):
            raise RuntimeError("official viewer state could not be reconstructed")
        return {
            "decision": max(1, int(decision)),
            "state_index": state_index,
            "action_count": total,
            "mazebench_version": data.get("mazebench_version"),
            "render_state": snapshots[state_index],
        }

    def capabilities(self) -> dict[str, Any]:
        playwright = self.replay_runtime / "node_modules" / "playwright-core"
        three_module = self.replay_runtime / "node_modules" / "three" / "build" / "three.module.js"
        bundled_three = self.replay_runtime / "vendor" / "three.module.js"
        three = three_module.exists() or bundled_three.exists()
        return {
            "mazebench_version": self.official["version"],
            "benchmark_contract_status": self.official_benchmark_contract["status"],
            "prompt_contract": self.official_benchmark_contract["prompt"],
            "official_profile_ready": (
                self.official["version"] == self.official["expected_version"]
            ),
            "replay_exporter": self.replay_script.exists(),
            "playwright_core": playwright.exists(),
            "three": three,
            "node": shutil.which("node") is not None,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "replay_ready": self.replay_script.exists()
            and playwright.exists()
            and three
            and shutil.which("node") is not None
            and shutil.which("ffmpeg") is not None,
            "world_browser": True,
            "room_builder": True,
            "experiment_batch_delete": True,
            "model_profiles": [
                public_model_profile(profile)
                for profile in self.model_profiles.values()
            ],
            "context_modes": list(CONTEXT_MODES.values()),
            "observation_modes": list(OBSERVATION_MODES.values()),
            "representation_contract_version": 2,
            "system_prompts": {
                "ascii_hidden": delivered_system_prompt(
                    hide_names=True, observation_mode="ascii"
                ),
                "ascii_visible": delivered_system_prompt(
                    hide_names=False, observation_mode="ascii"
                ),
                "json_hidden": delivered_system_prompt(
                    hide_names=True, observation_mode="json"
                ),
                "json_visible": delivered_system_prompt(
                    hide_names=False, observation_mode="json"
                ),
            },
        }

    def _new_job(self, kind: str, run_id: str) -> dict[str, Any]:
        job_id = f"{kind}-{run_id}-{int(time.time() * 1000)}"
        job = {
            "id": job_id,
            "kind": kind,
            "run_id": run_id,
            "status": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "ended_at": None,
            "error": None,
        }
        self.jobs[job_id] = job
        return job

    def start_trial(self, config: dict[str, Any]) -> dict[str, Any]:
        actions = int(config.get("actions", 256))
        temperature = float(config.get("temperature", 0.0))
        repetitions = int(config.get("repetitions") or 1)
        sampling_seed = int(config.get("sampling_seed") or 1)
        default_profile = next(iter(self.model_profiles))
        model_profile_id = str(config.get("model_profile") or default_profile).strip()
        context_mode = str(
            config.get("context_mode") or "generic-autocompact"
        ).strip()
        observation_mode = str(config.get("observation_mode") or "ascii").strip()
        if model_profile_id not in self.model_profiles:
            raise ValueError("unknown model profile")
        if context_mode not in CONTEXT_MODES:
            raise ValueError("unknown context-management mode")
        if observation_mode not in OBSERVATION_MODES:
            raise ValueError("unknown observation mode")
        model_profile = self.model_profiles[model_profile_id]
        model = str(model_profile["api_model"])
        base_url = str(model_profile["base_url"])
        level = str(config.get("level") or "").strip()
        submitted_character_mode = str(
            config.get("ascii_character_mode") or ""
        ).strip().lower()
        if not submitted_character_mode:
            # Compatibility for older API clients. MazeBench's canonical
            # characters are the new Control Center default.
            legacy_hide = config.get("hide_names")
            submitted_character_mode = (
                "random"
                if legacy_hide is not None
                and (
                    legacy_hide is True
                    or str(legacy_hide).lower() in {"1", "true", "on", "yes"}
                )
                else "canonical"
            )
        if submitted_character_mode not in {"canonical", "random"}:
            raise ValueError("ASCII character mode must be canonical or random")
        ascii_character_mode: str | None = (
            submitted_character_mode if observation_mode == "ascii" else None
        )
        hide_names = (
            observation_mode == "ascii" and submitted_character_mode == "random"
        )
        hide_names_seed = (
            str(config.get("hide_names_seed") or "1").strip()
            if hide_names
            else ""
        )
        thinking_value = config.get("thinking", False)
        thinking = thinking_value is True or str(thinking_value).lower() in {"1", "true", "on", "yes"}
        thinking_budget = int(config.get("thinking_budget", 2048))
        preserve_value = config.get("preserve_thinking", False)
        preserve_thinking = preserve_value is True or str(preserve_value).lower() in {
            "1", "true", "on", "yes"
        }
        if thinking and model_profile["thinking_contract"] == "none":
            raise ValueError("the selected model profile does not support thinking controls")
        unofficial_value = config.get("unofficial_system_prompt", False)
        unofficial_system_prompt = (
            unofficial_value is True
            or str(unofficial_value).lower() in {"1", "true", "on", "yes"}
        )
        submitted_system_prompt = str(config.get("system_prompt") or "")
        fork_parent_run_id = str(config.get("fork_parent_run_id") or "").strip()
        fork_turn_value = config.get("fork_turn")
        fork_turn = int(fork_turn_value) if fork_parent_run_id else None
        if not 1 <= actions <= 100_000:
            raise ValueError("actions must be between 1 and 100000")
        if not 0 <= temperature <= 5:
            raise ValueError("temperature must be between 0 and 5")
        if not 1 <= repetitions <= 100:
            raise ValueError("repetitions must be between 1 and 100")
        if not 0 <= sampling_seed <= 2_147_483_647 - repetitions + 1:
            raise ValueError("sampling seed range is invalid")
        if level and len(level) > 64:
            raise ValueError("level is too long")
        if not 64 <= thinking_budget <= 32_768:
            raise ValueError("thinking_budget must be between 64 and 32768")
        if hide_names and not hide_names_seed:
            raise ValueError("a random ASCII character seed is required")
        if len(hide_names_seed) > 128 or "\x00" in hide_names_seed:
            raise ValueError("ASCII character seed must be at most 128 characters")

        fork_plan = None
        if fork_parent_run_id:
            parent = self.store.load(fork_parent_run_id, include_timeline=False)
            if not parent:
                raise ValueError("fork parent run does not exist")
            if parent.get("status") in {"running", "queued", "stopping"}:
                raise ValueError("wait for the parent run to finish before forking it")
            if fork_turn is None:
                raise ValueError("fork_turn is required when a parent run is selected")
            parent_config = dict(parent.get("config") or {})
            parent_hide = parent_config.get("hide_names")
            if not isinstance(parent_hide, bool):
                raise ValueError("fork parent does not record its identity condition")
            # An ASCII active-context continuation must retain its parent's
            # symbol mapping. JSON always uses literal object-type names and
            # therefore has no ASCII character condition or seed.
            if observation_mode == "ascii":
                hide_names = parent_hide
                ascii_character_mode = "random" if parent_hide else "canonical"
                hide_names_seed = (
                    str(parent_config.get("hide_names_seed") or "1")
                    if parent_hide
                    else ""
                )
            else:
                hide_names = False
                ascii_character_mode = None
                hide_names_seed = ""
            level = str(parent_config.get("level") or "").strip()

        if hide_names and (
            not hide_names_seed
            or len(hide_names_seed) > 128
            or "\x00" in hide_names_seed
        ):
            raise ValueError("fork parent has an invalid randomized-character seed")

        official_system_prompt = delivered_system_prompt(
            hide_names=hide_names,
            observation_mode=observation_mode,
        )
        run_system_prompt = (
            submitted_system_prompt
            if unofficial_system_prompt
            else official_system_prompt
        )
        if not run_system_prompt.strip():
            raise ValueError("system prompt must not be empty")
        if len(run_system_prompt) > 100_000:
            raise ValueError("system prompt must be at most 100000 characters")
        system_prompt_sha256 = hashlib.sha256(run_system_prompt.encode()).hexdigest()
        if fork_parent_run_id:
            fork_plan = load_fork_plan(
                self.store.path_for(fork_parent_run_id),
                turn=fork_turn,
                system_prompt=run_system_prompt,
            )

        with self.lock:
            if any(
                job["kind"] == "trial" and job["status"] in {"queued", "running", "stopping"}
                for job in self.jobs.values()
            ):
                raise ValueError("a trial batch is already active; stop it or wait for it to finish")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        identity_label = (
            "literal"
            if observation_mode == "json"
            else ("random" if hide_names else "canonical")
        )
        suffix = "fork" if fork_plan else (
            (
                f"unofficial-{observation_mode}-{identity_label}"
            )
            if unofficial_system_prompt
            else (
                f"official-{observation_mode}-{identity_label}"
            )
        )
        with self.lock:
            experiment_id = f"experiment-{stamp}-{suffix}"
            jobs: list[dict[str, Any]] = []
            configs: list[dict[str, Any]] = []
            planned_ids: set[str] = set()
            worker_config = {
                "actions": actions,
                "temperature": temperature,
                "model": model,
                "model_profile": model_profile_id,
                "model_label": model_profile["label"],
                "base_url": base_url,
                "api_key_env": model_profile["api_key_env"],
                "token_count_mode": model_profile["token_count_mode"],
                "thinking_contract": model_profile["thinking_contract"],
                "context_mode": context_mode,
                "observation_mode": observation_mode,
                "ascii_character_mode": ascii_character_mode,
                "level": level,
                "profile": (
                    f"unofficial-{observation_mode}-prompt-{identity_label}"
                    if unofficial_system_prompt
                    else f"official-{observation_mode}-{identity_label}-native-contract"
                ),
                "system_prompt": "unofficial" if unofficial_system_prompt else "official",
                "system_prompt_sha256": system_prompt_sha256,
                "system_prompt_matches_official": run_system_prompt == official_system_prompt,
                "system_prompt_text": run_system_prompt,
                "hide_names": hide_names,
                "hide_names_seed": hide_names_seed,
                "thinking": thinking,
                "thinking_budget": thinking_budget if thinking else 0,
                "preserve_thinking": preserve_thinking if thinking else False,
                "fork": (
                    {
                        "mode": "active-context",
                        "parent_run_id": fork_plan.parent_run_id,
                        "turn": fork_plan.turn,
                    }
                    if fork_plan
                    else None
                ),
            }
            for repeat_index in range(1, repetitions + 1):
                repeat_suffix = f"-r{repeat_index:02d}" if repetitions > 1 else ""
                run_id = f"run-{stamp}-{suffix}{repeat_suffix}"
                counter = 2
                while self.store.path_for(run_id).exists() or run_id in planned_ids:
                    run_id = f"run-{stamp}-{suffix}{repeat_suffix}-{counter}"
                    counter += 1
                planned_ids.add(run_id)
                job = self._new_job("trial", run_id)
                job.update(
                    experiment_id=experiment_id,
                    repeat_index=repeat_index,
                    repeat_count=repetitions,
                    sampling_seed=sampling_seed + repeat_index - 1,
                    model_label=model_profile["label"],
                )
                if fork_plan:
                    job["fork_parent_run_id"] = fork_plan.parent_run_id
                repeat_config = {
                    **worker_config,
                    "sampling_seed": sampling_seed + repeat_index - 1,
                    "experiment_id": experiment_id,
                    "repeat_index": repeat_index,
                    "repeat_count": repetitions,
                }
                jobs.append(job)
                configs.append(repeat_config)
            thread = threading.Thread(
                target=self._trial_worker if repetitions == 1 else self._trial_batch_worker,
                args=(
                    (jobs[0]["id"], configs[0])
                    if repetitions == 1
                    else ([job["id"] for job in jobs], configs)
                ),
                daemon=True,
                name=experiment_id,
            )
            for job in jobs:
                job["thread"] = thread
            thread.start()
            response = public_job(jobs[0])
            response["run_ids"] = [job["run_id"] for job in jobs]
            return response

    def _trial_batch_worker(
        self, job_ids: list[str], configs: list[dict[str, Any]]
    ) -> None:
        for job_id, config in zip(job_ids, configs, strict=True):
            with self.lock:
                if self.jobs[job_id]["status"] != "queued":
                    continue
            self._trial_worker(job_id, config)

    def _trial_worker(self, job_id: str, config: dict[str, Any]) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", started_at=utc_now())
        run_id = job["run_id"]
        run_dir = self.store.path_for(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        requested_prompt_path = run_dir / "requested-system-prompt.txt"
        requested_prompt_path.write_text(config["system_prompt_text"], encoding="utf-8")
        public_config = {
            key: value
            for key, value in config.items()
            if key != "system_prompt_text"
        }
        write_json(
            run_dir / "run.json",
            {
                "schema_version": 5,
                "id": run_id,
                "status": "running",
                "created_at": utc_now(),
                "config": public_config,
            },
        )
        runner_command = [
            sys.executable,
            "-m",
            "mblab.smoke",
            "--actions", str(config["actions"]),
            "--base-url", config["base_url"],
            "--model", config["model"],
            "--api-key-env", config["api_key_env"],
            "--token-count-mode", config["token_count_mode"],
            "--thinking-contract", config["thinking_contract"],
            "--context-mode", config["context_mode"],
            "--observation-mode", config["observation_mode"],
            "--temperature", str(config["temperature"]),
            "--seed", str(config["sampling_seed"]),
            "--hide-names", "on" if config["hide_names"] else "off",
            "--hide-names-seed", config["hide_names_seed"],
            "--thinking", "on" if config["thinking"] else "off",
            "--thinking-budget", str(config["thinking_budget"] or 2048),
            "--preserve-thinking", "on" if config["preserve_thinking"] else "off",
            "--system-prompt-mode", config["system_prompt"],
            "--system-prompt-file", str(requested_prompt_path),
            "--out", str(self.store.root),
            "--run-id", run_id,
        ]
        if config["level"]:
            runner_command += ["--level", config["level"]]
        if config.get("fork"):
            runner_command += [
                "--fork-parent",
                config["fork"]["parent_run_id"],
                "--fork-turn",
                str(config["fork"]["turn"]),
            ]
        environment = os.environ.copy()
        source = SOURCE_ROOT / "src"
        if source.is_dir():
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(source)
                if not existing_pythonpath
                else os.pathsep.join((str(source), existing_pythonpath))
            )
        log_path = run_dir / "runner.log"
        model_log_path = run_dir / "model-server.log"
        model_process: subprocess.Popen[Any] | None = None
        model_was_launched = False
        try:
            model_profile = self.model_profiles[config["model_profile"]]
            provider = OpenAICompatibleService(model_profile, self.repo_root)
            existing_models = provider.models()
            with model_log_path.open("w") as model_log:
                if existing_models is None:
                    model_process = provider.launch(
                        model_log,
                        base_environment=environment,
                    )
                    model_was_launched = True
                    with self.lock:
                        self.jobs[job_id].update(
                            model_process=model_process,
                            phase="loading_model",
                        )
                    models = provider.wait_ready(
                        model_process,
                        expected_model=config["model"],
                        should_stop=lambda: self.jobs[job_id]["status"] == "stopping",
                    )
                else:
                    models = existing_models
                    if config["model"] not in models:
                        reported = ", ".join(models) if models else "no models"
                        raise RuntimeError(
                            f"{config['base_url']} reports an incompatible "
                            f"model service ({reported}); stop it before selecting "
                            f"{config['model']}"
                        )
                    model_log.write(
                        "Reused an already-running compatible local model service.\n"
                    )
                    model_log.flush()
                write_json(
                    run_dir / "model-service.json",
                    {
                        "profile": config["model_profile"],
                        "model": config["model"],
                        "base_url": config["base_url"],
                        "launched_by_control_center": model_was_launched,
                        "reported_models": models,
                        "ready_at": utc_now(),
                    },
                )
                with self.lock:
                    self.jobs[job_id]["phase"] = "running_trial"
                with log_path.open("w") as log:
                    process = subprocess.Popen(
                        runner_command,
                        cwd=self.repo_root,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    with self.lock:
                        self.jobs[job_id]["process"] = process
                    return_code = process.wait()
            if return_code:
                raise RuntimeError(f"runner exited with status {return_code}")
            status = "completed"
            error = None
        except Exception as exc:  # the log contains the detailed traceback
            status = "failed"
            error = str(exc)
            manifest = read_json(run_dir / "run.json", {})
            manifest.update(
                schema_version=5,
                id=run_id,
                status=status,
                ended_at=utc_now(),
                error=error,
                config=config,
            )
            write_json(run_dir / "run.json", manifest)
        finally:
            if model_was_launched:
                terminate_process(model_process)
            service = read_json(run_dir / "model-service.json", {})
            if service:
                service.update(
                    released_at=utc_now(),
                    stopped_by_control_center=model_was_launched,
                )
                write_json(run_dir / "model-service.json", service)
            manifest = read_json(run_dir / "run.json", {})
            if manifest:
                manifest_config = manifest.setdefault("config", {})
                manifest_config.update(
                    model_profile=config["model_profile"],
                    model_label=config["model_label"],
                )
                artifacts = manifest.setdefault("artifacts", [])
                for name in ("model-server.log", "model-service.json"):
                    if (run_dir / name).exists() and name not in artifacts:
                        artifacts.append(name)
                write_json(run_dir / "run.json", manifest)
        with self.lock:
            self.jobs[job_id].update(
                status=status,
                phase="finished",
                ended_at=utc_now(),
                error=error,
            )
            self.jobs[job_id].pop("process", None)
            self.jobs[job_id].pop("model_process", None)

    def start_replay(self, run_id: str) -> dict[str, Any]:
        run = self.store.load(run_id)
        if not run:
            raise FileNotFoundError(run_id)
        if not run["artifacts"]["actions"]:
            raise ValueError("this run has no recorded actions")
        with self.lock:
            for job in self.jobs.values():
                if job["kind"] == "replay" and job["run_id"] == run_id and job["status"] in {"queued", "running"}:
                    return public_job(job)
            job = self._new_job("replay", run_id)
            thread = threading.Thread(
                target=self._replay_worker,
                args=(job["id"],),
                daemon=True,
                name=job["id"],
            )
            job["thread"] = thread
            thread.start()
            return public_job(job)

    def delete_run(self, run_id: str) -> dict[str, Any]:
        path = self.store.path_for(run_id)
        if not path.is_dir():
            raise FileNotFoundError(run_id)
        if read_json(path / "run.json", {}).get("status") == "running":
            raise ValueError("a running experiment cannot be deleted")
        with self.lock:
            if any(
                (
                    job["run_id"] == run_id
                    or job.get("fork_parent_run_id") == run_id
                )
                and job["status"] in {"queued", "running", "stopping"}
                for job in self.jobs.values()
            ):
                raise ValueError("an active run, replay, or child fork depends on this run")
        trash = self.store.root / ".trash"
        trash.mkdir(exist_ok=True)
        target = trash / f"{run_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        counter = 2
        while target.exists():
            target = trash / f"{run_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{counter}"
            counter += 1
        shutil.move(str(path), str(target))
        return {"deleted": run_id, "recoverable_from": str(target)}

    def delete_experiment(self, experiment_id: str) -> dict[str, Any]:
        """Move every persisted repetition in a completed batch to trash."""
        experiment_id = str(experiment_id or "").strip()
        if not RUN_ID.fullmatch(experiment_id):
            raise ValueError("invalid experiment id")
        runs = [
            run for run in self.store.list()
            if str((run.get("config") or {}).get("experiment_id") or "") == experiment_id
        ]
        if not runs:
            raise FileNotFoundError(experiment_id)
        run_ids = {run["id"] for run in runs}
        with self.lock:
            if any(
                job.get("experiment_id") == experiment_id
                and job.get("status") in {"queued", "running", "stopping"}
                for job in self.jobs.values()
            ):
                raise ValueError("an active experiment batch cannot be deleted")
            if any(
                (job.get("run_id") in run_ids or job.get("fork_parent_run_id") in run_ids)
                and job.get("status") in {"queued", "running", "stopping"}
                for job in self.jobs.values()
            ):
                raise ValueError("an active run, replay, or child fork depends on this experiment")
        deleted = [self.delete_run(run["id"]) for run in runs]
        return {
            "deleted_experiment": experiment_id,
            "deleted_runs": [item["deleted"] for item in deleted],
            "recoverable_from": [item["recoverable_from"] for item in deleted],
        }

    def _replay_worker(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", started_at=utc_now())
        run_id = job["run_id"]
        run_dir = self.store.path_for(run_id)
        output_dir = run_dir / "artifacts"
        output_dir.mkdir(exist_ok=True)
        # Render away from the currently-served MP4, then atomically publish
        # the finished artifact. This prevents regeneration from exposing a
        # partially-written file to an open browser video element.
        render_dir = Path(
            tempfile.mkdtemp(prefix=".replay-render-", dir=output_dir)
        )
        source = run_dir / "rollout.jsonl"
        if not source.exists():
            actions = read_json(run_dir / "actions.json", [])
            first_status = (actions[0].get("status") or {}) if actions else {}
            row = {
                "maze_actions": actions,
                "maze_replay": {
                    "game_id": "maze",
                    "start_level_id": first_status.get("current_room", "level_HxI"),
                    "actions": actions,
                },
            }
            source = output_dir / "replay-source.jsonl"
            source.write_text(json.dumps(row, default=str) + "\n")
        command = [
            shutil.which("node") or "node",
            str(self.replay_script),
            str(source),
            "--out-dir", str(render_dir),
            "--draft",
            # Analysis profile: keep movement legible and the whole room in
            # frame. --fast used to emit one frame per action (a 30-action run
            # lasted only two seconds). MazeBench renders fixed-size tiles, so
            # the larger 4:3 viewport prevents its player-following viewport
            # from clipping the room at video edges.
            "--intro",
            "--width", "1280",
            "--height", "960",
            "--fps", "24",
            "--move-speed", "1",
            "--camera-speed", "1",
            "--motion-scale", "4",
            "--camera-tilt", "45",
            # With Three.js active, zoom 1 fits the complete current room at a
            # readable scale. The former 0.28 value exposed the surrounding
            # world and made the active room too small.
            "--camera-zoom", "1",
            "--tail-seconds", "1.25",
        ]
        log_path = output_dir / "replay.log"
        try:
            capabilities = self.capabilities()
            if not capabilities["replay_ready"]:
                missing = ", ".join(
                    key
                    for key in ("node", "ffmpeg", "playwright_core", "three")
                    if not capabilities[key]
                )
                raise RuntimeError(f"3D replay dependency missing: {missing}; run scripts/setup-replay.sh")
            replay_environment = os.environ.copy()
            # The official exporter records the exact frame interval belonging
            # to each action when diagnostics are enabled. Retain that small
            # manifest so the browser can seek the MP4 to the observation
            # selected in the model-input trace.
            replay_environment["MAZE_REPLAY_DIAGNOSTICS"] = "1"
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    command,
                    cwd=self.replay_runtime,
                    env=replay_environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                with self.lock:
                    self.jobs[job_id]["process"] = process
                return_code = process.wait()
            if return_code:
                raise RuntimeError(f"replay exporter exited with status {return_code}; see artifacts/replay.log")
            rendered_video = render_dir / "maze_replay.mp4"
            if not rendered_video.is_file():
                raise RuntimeError("replay exporter completed without producing an MP4")
            for name in (
                "maze_scorecard.json",
                "maze_actions.txt",
                "maze_replay.mp4",
                ".maze_replay_manifest.json",
            ):
                rendered = render_dir / name
                if rendered.exists():
                    rendered.replace(output_dir / name)
            status, error = "completed", None
        except Exception as exc:
            status, error = "failed", str(exc)
            with log_path.open("a") as log:
                log.write(error + "\n")
        with self.lock:
            self.jobs[job_id].update(status=status, ended_at=utc_now(), error=error)
            self.jobs[job_id].pop("process", None)
        shutil.rmtree(render_dir, ignore_errors=True)

    def stop_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise FileNotFoundError(job_id)
            experiment_id = job.get("experiment_id")
            related = [
                candidate for candidate in self.jobs.values()
                if candidate is job or (experiment_id and candidate.get("experiment_id") == experiment_id)
            ]
            for candidate in related:
                if candidate.get("status") == "queued":
                    candidate.update(status="cancelled", ended_at=utc_now())
                elif candidate.get("status") == "running":
                    candidate.update(status="stopping")
                for key in ("process", "model_process"):
                    process = candidate.get(key)
                    if process and process.poll() is None:
                        process.terminate()
            return public_job(job)

    def shutdown(self) -> None:
        """Stop only child processes launched by this control-center process."""
        with self.lock:
            processes = [
                process
                for job in self.jobs.values()
                for process in (job.get("process"), job.get("model_process"))
                if process is not None
            ]
        for process in processes:
            terminate_process(process)
        self.world_service.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "MazeBenchControlCenter/0.1"

    @property
    def state(self) -> ControlState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, message: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {message % args}")

    def json_response(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, status: int, message: str) -> None:
        self.json_response({"error": message}, status)

    def request_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65_536:
            raise ValueError("request body is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def proxy_runtime(self) -> bool:
        """Proxy MazeBench's native authoring site below /maze."""
        parsed_url = urllib.parse.urlsplit(self.path)
        if parsed_url.path not in {"/maze", "/maze/"} and not parsed_url.path.startswith("/maze/"):
            return False
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.error_response(403, "the room builder is available only over a loopback connection")
            return True
        target_path = parsed_url.path.removeprefix("/maze") or "/build"
        if parsed_url.query:
            target_path += f"?{parsed_url.query}"
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 25 * 1024 * 1024:
            self.error_response(413, "MazeBench builder request is too large")
            return True
        body = self.rfile.read(length) if length else b""
        try:
            status, headers, payload = self.state.world_service.proxy(
                self.command,
                target_path,
                body=body,
                headers={
                    "Accept": self.headers.get("Accept", ""),
                    "Content-Type": self.headers.get("Content-Type", ""),
                    "Range": self.headers.get("Range", ""),
                },
            )
        except Exception as exc:
            self.error_response(502, str(exc))
            return True
        self.send_response(status)
        for name in ("Content-Type", "Content-Disposition", "Accept-Ranges", "Content-Range", "Location"):
            if headers.get(name):
                self.send_header(name, headers[name])
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", headers.get("Cache-Control", "no-store"))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self.proxy_runtime():
            return
        parsed_url = urllib.parse.urlsplit(self.path)
        path = parsed_url.path
        if path == "/api/health":
            self.json_response(
                {
                    "ok": True,
                    "capabilities": self.state.capabilities(),
                    "world_rooms": self.state.world_rooms,
                }
            )
            return
        if path == "/api/runs":
            with self.state.lock:
                jobs = [public_job(job) for job in self.state.jobs.values()]
            self.json_response({"runs": self.state.store.list(), "jobs": jobs})
            return
        if path == "/api/jobs":
            with self.state.lock:
                jobs = [public_job(job) for job in self.state.jobs.values()]
            self.json_response({"jobs": jobs})
            return
        if path == "/api/worlds":
            self.json_response(self.state.world_service.catalog())
            return
        parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
        if len(parts) == 5 and parts[:2] == ["api", "worlds"] and parts[3] == "rooms":
            self.json_response(self.state.world_service.room_detail(parts[2], parts[4]))
            return
        if len(parts) == 4 and parts[:3] == ["api", "play", "maze"]:
            self.json_response(self.state.level_state(parts[3]))
            return
        if len(parts) == 2 and parts[0] == "vendor":
            self.send_official_file(self.state.replay_runtime / "vendor" / parts[1])
            return
        if len(parts) >= 3 and parts[:2] == ["assets", "maze"]:
            self.send_official_file(
                self.state.replay_runtime / "games" / "maze" / Path(*parts[2:])
            )
            return
        if len(parts) == 2 and parts[0] == "official":
            self.send_official_file(self.state.replay_runtime / "public" / parts[1])
            return
        if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
            run_id = parts[2]
            if len(parts) == 3:
                run = self.state.store.load(run_id)
                if not run:
                    self.error_response(404, "run not found")
                else:
                    self.json_response(run)
                return
            if parts[3:] == ["interactions"]:
                interactions = self.state.store.load_interactions(run_id)
                compactions = self.state.store.load_compactions(run_id)
                self.json_response(
                    {"interactions": interactions, "compactions": compactions}
                )
                return
            if parts[3:] == ["viewer-snapshot"]:
                query = urllib.parse.parse_qs(parsed_url.query)
                decision = int((query.get("decision") or ["1"])[0])
                self.json_response(self.state.viewer_snapshot(run_id, decision))
                return
            if parts[3:] == ["replay.mp4"]:
                replay = self.state.store.replay_path(run_id)
                if not replay:
                    self.error_response(404, "replay not generated")
                else:
                    self.send_file(replay, allow_range=True)
                return
            if parts[3:] == ["log"]:
                run_dir = self.state.store.path_for(run_id)
                sections = []
                for label, candidate in (
                    ("MODEL SERVICE", run_dir / "model-server.log"),
                    ("TRIAL RUNNER", run_dir / "runner.log"),
                ):
                    if candidate.exists():
                        sections.append(
                            f"===== {label} =====\n"
                            + candidate.read_text(errors="replace")[-40_000:]
                        )
                if not sections:
                    self.error_response(404, "log not found")
                else:
                    self.json_response({"log": "\n\n".join(sections)})
                return
        self.serve_static(path)

    def send_official_file(self, path: Path) -> None:
        runtime = self.state.replay_runtime.resolve()
        candidate = path.resolve()
        if runtime not in candidate.parents or not candidate.is_file():
            self.error_response(404, "official runtime asset not found")
            return
        self.send_file(candidate)

    def do_POST(self) -> None:  # noqa: N802
        if self.proxy_runtime():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/runs":
                self.json_response(self.state.start_trial(self.request_json()), HTTPStatus.ACCEPTED)
                return
            parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "replay":
                self.json_response(self.state.start_replay(parts[2]), HTTPStatus.ACCEPTED)
                return
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "stop":
                self.json_response(self.state.stop_job(parts[2]), HTTPStatus.ACCEPTED)
                return
            self.error_response(404, "endpoint not found")
        except FileNotFoundError:
            self.error_response(404, "resource not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.error_response(400, str(exc))
        except Exception as exc:
            self.error_response(500, str(exc))

    def do_DELETE(self) -> None:  # noqa: N802
        if self.proxy_runtime():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            parts = [urllib.parse.unquote(part) for part in path.split("/") if part]
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                self.json_response(self.state.delete_run(parts[2]))
                return
            if len(parts) == 3 and parts[:2] == ["api", "experiments"]:
                self.json_response(self.state.delete_experiment(parts[2]))
                return
            self.error_response(404, "endpoint not found")
        except FileNotFoundError:
            self.error_response(404, "run not found")
        except ValueError as exc:
            self.error_response(409, str(exc))
        except Exception as exc:
            self.error_response(500, str(exc))

    def do_PUT(self) -> None:  # noqa: N802
        if not self.proxy_runtime():
            self.error_response(404, "endpoint not found")

    def do_PATCH(self) -> None:  # noqa: N802
        if not self.proxy_runtime():
            self.error_response(404, "endpoint not found")

    def serve_static(self, request_path: str) -> None:
        candidate = web_asset_for(request_path)
        if candidate is None or not candidate.is_file():
            self.error_response(404, "not found")
            return
        self.send_file(candidate)

    def send_file(self, path: Path, *, allow_range: bool = False) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if allow_range and (header := self.headers.get("Range")):
            try:
                unit, values = header.split("=", 1)
                left, right = values.split("-", 1)
                if unit != "bytes" or "," in values:
                    raise ValueError
                start = int(left) if left else max(0, size - int(right))
                end = int(right) if right else size - 1
                if start < 0 or end < start or end >= size:
                    raise ValueError
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(status)
        self.send_header("Content-Type", content_type_for(path))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MazeBench local control center")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--runs", default="runs")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML model-profile file (or set MAZEBENCH_CC_CONFIG)",
    )
    args = parser.parse_args(argv)
    workspace_root = Path.cwd().resolve()
    model_profiles = load_model_profiles(workspace_root, args.config)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.state = ControlState(  # type: ignore[attr-defined]
        workspace_root,
        Path(args.runs),
        model_profiles=model_profiles,
    )
    print(f"MazeBench control center: http://{args.host}:{args.port}")
    print(f"Runs: {Path(args.runs).resolve()}")
    print("Models: " + ", ".join(model_profiles))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping control center")
    finally:
        server.state.shutdown()  # type: ignore[attr-defined]
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
