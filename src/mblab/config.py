"""Portable configuration for model services used by the control center."""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any


PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "local-openai": {
        "id": "local-openai",
        "label": "Local OpenAI-compatible endpoint",
        "provider": "openai-compatible",
        "api_model": "local",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key_env": "MAZEBENCH_API_KEY",
        "token_count_mode": "estimate",
        "thinking_contract": "none",
        "launch_command": [],
        "launch_cwd": ".",
        "environment": {},
        "context_window": 65_536,
    }
}


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _command(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("model launch_command must be a string or list of strings")


def normalize_model_profile(
    profile_id: str,
    value: dict[str, Any],
    *,
    config_dir: Path,
) -> dict[str, Any]:
    if not PROFILE_ID.fullmatch(profile_id):
        raise ValueError(f"invalid model profile id: {profile_id!r}")
    api_model = str(value.get("api_model") or profile_id).strip()
    base_url = str(value.get("base_url") or "").rstrip("/")
    if not api_model or not base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"model profile {profile_id!r} requires api_model and an HTTP(S) base_url"
        )
    launch_command = [_expand(item) for item in _command(value.get("launch_command"))]
    if launch_command:
        executable = Path(launch_command[0])
        if not executable.is_absolute() and "/" in launch_command[0]:
            launch_command[0] = str((config_dir / executable).resolve())
    launch_cwd_text = _expand(str(value.get("launch_cwd") or "."))
    launch_cwd = Path(launch_cwd_text)
    if not launch_cwd.is_absolute():
        launch_cwd = config_dir / launch_cwd
    environment = value.get("environment") or {}
    if not isinstance(environment, dict):
        raise ValueError(f"model profile {profile_id!r} environment must be a table")
    context_window = int(value.get("context_window") or 0)
    if context_window < 0:
        raise ValueError(f"model profile {profile_id!r} context_window must be positive")
    provider = str(value.get("provider") or "openai-compatible")
    api_key_env = str(value.get("api_key_env") or "MAZEBENCH_API_KEY")
    if not ENVIRONMENT_NAME.fullmatch(api_key_env):
        raise ValueError(f"model profile {profile_id!r} has an invalid api_key_env")
    default_token_count_mode = "llama.cpp" if provider == "llama.cpp" else "estimate"
    token_count_mode = str(value.get("token_count_mode") or default_token_count_mode)
    if token_count_mode not in {"llama.cpp", "estimate"}:
        raise ValueError(f"model profile {profile_id!r} has an invalid token_count_mode")
    thinking_contract = str(value.get("thinking_contract") or "none")
    if thinking_contract not in {"qwen", "none"}:
        raise ValueError(f"model profile {profile_id!r} has an invalid thinking_contract")
    return {
        "id": profile_id,
        "label": str(value.get("label") or profile_id),
        "provider": provider,
        "api_model": api_model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "token_count_mode": token_count_mode,
        "thinking_contract": thinking_contract,
        "launch_command": launch_command,
        "launch_cwd": str(launch_cwd.resolve()),
        "environment": {
            str(key): _expand(str(item)) for key, item in environment.items()
        },
        "context_window": context_window or None,
    }


def default_model_profiles(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Return a safe unmanaged endpoint for first-run UI discovery."""
    return {
        profile_id: normalize_model_profile(
            profile_id,
            value,
            config_dir=repo_root,
        )
        for profile_id, value in DEFAULT_MODEL_PROFILES.items()
    }


def load_model_profiles(
    repo_root: Path,
    config_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load a TOML profile file or return the neutral local endpoint profile.

    ``MAZEBENCH_CC_CONFIG`` is consulted only when no explicit path is supplied. A
    supplied file replaces the built-in profiles so experiments cannot
    silently select a machine-specific launcher.
    """
    selected = config_path
    if selected is None and os.environ.get("MAZEBENCH_CC_CONFIG"):
        selected = Path(_expand(os.environ["MAZEBENCH_CC_CONFIG"]))
    if selected is None:
        return default_model_profiles(repo_root)
    selected = selected.expanduser().resolve()
    try:
        document = tomllib.loads(selected.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"configuration file does not exist: {selected}") from None
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid TOML configuration {selected}: {error}") from None
    models = document.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("configuration must contain at least one [models.<id>] table")
    return {
        str(profile_id): normalize_model_profile(
            str(profile_id),
            dict(value),
            config_dir=selected.parent,
        )
        for profile_id, value in models.items()
        if isinstance(value, dict)
    }


def public_model_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Return capabilities without exposing commands or environment values."""
    return {
        key: value
        for key, value in profile.items()
        if key not in {"launch_command", "launch_cwd", "environment"}
    } | {"managed": bool(profile.get("launch_command"))}
