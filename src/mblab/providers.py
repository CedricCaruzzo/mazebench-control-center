"""Inference-service lifecycle adapters.

The benchmark runner speaks OpenAI-compatible chat completions. This module
isolates how an endpoint is discovered or launched from the experiment logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, IO


class OpenAICompatibleService:
    def __init__(self, profile: dict[str, Any], repo_root: Path):
        self.profile = profile
        self.repo_root = repo_root.resolve()
        self.base_url = str(profile["base_url"]).rstrip("/")

    def models(self, timeout: float = 2) -> list[str] | None:
        try:
            key_name = str(self.profile.get("api_key_env") or "")
            key = os.environ.get(key_name) if key_name else None
            if not key and key_name:
                key = str((self.profile.get("environment") or {}).get(key_name) or "")
            request = urllib.request.Request(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {key or 'none'}"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            return [
                str(item.get("id"))
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        except Exception:
            return None

    def launch(
        self,
        log: IO[str],
        *,
        base_environment: dict[str, str] | None = None,
    ) -> subprocess.Popen[Any]:
        command = list(self.profile.get("launch_command") or [])
        if not command:
            raise RuntimeError(
                "model endpoint is unavailable and this profile has no launch_command"
            )
        first = Path(command[0])
        if not first.is_absolute() and "/" in command[0]:
            command[0] = str((self.repo_root / first).resolve())
        environment = dict(base_environment or os.environ)
        environment.update(
            {str(key): str(value) for key, value in (self.profile.get("environment") or {}).items()}
        )
        environment["MODEL_ALIAS"] = str(self.profile["api_model"])
        cwd = Path(str(self.profile.get("launch_cwd") or self.repo_root)).resolve()
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(
        self,
        process: subprocess.Popen[Any],
        *,
        expected_model: str,
        should_stop: Callable[[], bool] | None = None,
        timeout_seconds: int = 600,
    ) -> list[str]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            models = self.models()
            if models is not None:
                if expected_model in models:
                    return models
                if models:
                    raise RuntimeError(
                        "model server became ready with an unexpected model: "
                        + ", ".join(models)
                    )
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"model server exited with status {return_code}; see model-server.log"
                )
            if should_stop and should_stop():
                raise RuntimeError("trial stopped while the model was loading")
            time.sleep(1)
        raise RuntimeError(
            f"model server did not become ready within {timeout_seconds} seconds"
        )


def terminate_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
