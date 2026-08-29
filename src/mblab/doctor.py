"""Portable, read-only installation diagnostics."""

from __future__ import annotations

import platform
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from mblab.config import load_model_profiles
from mblab.providers import OpenAICompatibleService


def diagnostics(repo_root: Path, config_path: Path | None = None) -> dict[str, Any]:
    profiles = load_model_profiles(repo_root, config_path)
    packages = {}
    for name in ("mazebench", "verifiers", "httpx"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    models = []
    for profile in profiles.values():
        service = OpenAICompatibleService(profile, repo_root)
        command = list(profile.get("launch_command") or [])
        executable = command[0] if command else None
        executable_ready = bool(
            executable
            and (
                Path(executable).is_file()
                or shutil.which(executable) is not None
            )
        )
        models.append(
            {
                "id": profile["id"],
                "provider": profile["provider"],
                "base_url": profile["base_url"],
                "reported_models": service.models(timeout=0.5),
                "managed": bool(command),
                "launcher_ready": executable_ready if command else None,
            }
        )
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": packages,
        "binaries": {
            name: shutil.which(name)
            for name in ("llama-server", "node", "npm", "ffmpeg")
        },
        "models": models,
    }


def print_diagnostics(report: dict[str, Any]) -> bool:
    print(f"Python {report['python']} · {report['platform']}")
    for name, installed in report["packages"].items():
        print(f"package {name:10} {installed or 'MISSING'}")
    for name, path in report["binaries"].items():
        print(f"binary  {name:10} {path or 'MISSING'}")
    for model in report["models"]:
        endpoint = (
            ", ".join(model["reported_models"])
            if model["reported_models"] is not None
            else "offline"
        )
        print(
            f"model   {model['id']:10} {model['provider']} · {endpoint} · "
            f"managed={model['managed']} launcher_ready={model['launcher_ready']}"
        )
    return bool(report["packages"].get("mazebench") and report["packages"].get("verifiers"))
