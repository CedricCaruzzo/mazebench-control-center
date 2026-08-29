"""Locate and verify the official MazeBench runtime installed from PyPI."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


PINNED_MAZEBENCH_VERSION = "0.2.18"

# This is the hidden-identity ASCII observation paragraph delivered by the
# official native game-agent harness in scripts/maze-agent-local.js. The hosted
# multiturn compatibility adapter has a command-format system prompt but omits
# this observation-mode layer, so local chat-model runs must add it explicitly.
# Keep this text source-aligned and fail closed below if the pinned package no
# longer contains it.
OFFICIAL_HIDDEN_ASCII_INSTRUCTION = """This is ASCII mode. Observations contain the current room's ASCII
board in the level field and the complete visited_levels list. Directional
glyphs are relative to the current camera. Every glyph except player P and gem
G is assigned a stable random identity for this run. Infer meanings only from
observations and interactions in this run."""

OFFICIAL_VISIBLE_ASCII_INSTRUCTION = """This is ASCII mode. Observations contain the current room's ASCII
board in the level field, any dynamic Unicode clone/box symbols in ascii_legend,
and the complete visited_levels list. Directional slope and puncher glyphs are
relative to the current camera."""

OFFICIAL_HIDDEN_JSON_INSTRUCTION = """This is JSON mode. Read json_observation.objects instead of an ASCII
board. Coordinates are [x,y,elevation]. Object types have stable random letter
IDs for this run; player and gem remain literal. Infer every hidden type only
from the observations and interactions in this run."""

OFFICIAL_VISIBLE_JSON_INSTRUCTION = """This is JSON mode. Read json_observation.objects instead of an ASCII
board. Schema version 2 coordinates are [x,y,elevation]. Directional object
names such as ice_slope_up, black_ice_slope_left, orange_ice_slope_down,
puncher_left, ramped_clone_c7_up, and ramped_weightless_push_box_M7_right are
relative to the current camera. Clone and weightless-push-box names preserve
their arbitrary group ids. Player lifts and attached lifts/gates include their
live raised/lowered state in the name. Orange walls and orange ice slopes drop
one elevation while the orange buttons are pressed. Only objects with at least
one character visible in the equivalent ASCII view are included, so rotate the
camera to reveal occluded objects. Object type names are literal."""

BENCHMARK_DEFAULTS = {
    "game_id": "maze",
    "start_level_id": "level_HxI",
    "view": "top-diagonal",
    "yaw": 0,
    "game_won_gem_count": 100,
    "target_gems": 0,
    "gem_reward_weight": 1.0,
    "room_reward_weight": 0.1,
    "push_reward_weight": 0.05,
    "observation_mode": "ascii",
    "omniscient": False,
    "allow_quit": True,
    "auto_quit": False,
}


def official_runtime_root() -> Path:
    spec = importlib.util.find_spec("mazebench_cli")
    if spec is None or spec.origin is None:
        raise RuntimeError("MazeBench CLI is not installed in this environment")
    runtime = Path(spec.origin).resolve().parent / "_runtime"
    if not (runtime / "scripts" / "maze-bridge.js").is_file():
        raise RuntimeError(f"official MazeBench runtime is incomplete: {runtime}")
    return runtime


def official_environment_root() -> Path:
    root = official_runtime_root() / "environments" / "mazebench"
    if not (root / "mazebench" / "__init__.py").is_file():
        raise RuntimeError(f"official MazeBench Python environment is missing: {root}")
    return root


def activate_official_environment() -> Path:
    """Expose the wheel's nested official environment without copying it."""
    root = official_environment_root()
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return root


def prompt_path() -> Path:
    return official_environment_root() / "mazebench" / "prompts" / "multiturn_system.txt"


def native_agent_prompt_path() -> Path:
    return official_runtime_root() / "scripts" / "maze-agent-local.js"


def glyph_contract_path() -> Path:
    return official_runtime_root() / "shared" / "maze-observation-contract.js"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalized_whitespace(value: str) -> str:
    return " ".join(value.split())


def base_system_prompt() -> str:
    return prompt_path().read_text(encoding="utf-8").strip()


def official_observation_instruction(
    *, observation_mode: str, hide_names: bool
) -> str:
    mode = str(observation_mode).lower()
    if mode == "ascii":
        return (
            OFFICIAL_HIDDEN_ASCII_INSTRUCTION
            if hide_names
            else OFFICIAL_VISIBLE_ASCII_INSTRUCTION
        )
    if mode == "json":
        return (
            OFFICIAL_HIDDEN_JSON_INSTRUCTION
            if hide_names
            else OFFICIAL_VISIBLE_JSON_INSTRUCTION
        )
    raise ValueError("observation_mode must be ascii or json")


def delivered_system_prompt(
    *, hide_names: bool, observation_mode: str = "ascii"
) -> str:
    """Compose official prompt layers for the hosted compatibility route."""
    base = base_system_prompt()
    instruction = official_observation_instruction(
        observation_mode=observation_mode,
        hide_names=hide_names,
    )
    return f"{base}\n\n{instruction}"


def benchmark_contract(
    *, hide_names: bool, observation_mode: str = "ascii"
) -> dict[str, Any]:
    """Fail closed on benchmark-critical package and prompt drift.

    This verifies the pinned official source rather than trusting our copied
    constants. It deliberately does not claim native-harness equivalence: the
    local lab uses the official hosted multiturn adapter over llama.cpp.
    """
    official = provenance()
    checks: dict[str, bool] = {}
    checks["pinned_package_version"] = (
        official["version"] == PINNED_MAZEBENCH_VERSION
    )

    launcher = native_agent_prompt_path().read_text(encoding="utf-8")
    observation_instruction = official_observation_instruction(
        observation_mode=observation_mode,
        hide_names=hide_names,
    )
    checks["native_hidden_ascii_instruction_present"] = (
        _normalized_whitespace(OFFICIAL_HIDDEN_ASCII_INSTRUCTION)
        in _normalized_whitespace(launcher)
    )
    # The visible JSON paragraph contains an official runtime interpolation for
    # omniscience. Its invariant opening sentence is still static source text.
    instruction_probe = observation_instruction.split(".", 1)[0] + "."
    checks["selected_native_observation_instruction_present"] = (
        _normalized_whitespace(instruction_probe)
        in _normalized_whitespace(launcher)
    )

    glyph_source = glyph_contract_path().read_text(encoding="utf-8")
    checks["player_glyph_fixed"] = '["P", "P"]' in glyph_source
    checks["player_side_glyph_fixed"] = '["p", "p"]' in glyph_source
    checks["gem_glyph_fixed"] = '["G", "G"]' in glyph_source
    checks["gem_side_glyph_fixed"] = '["g", "g"]' in glyph_source
    checks["other_hidden_glyphs_permuted"] = (
        "function hiddenAsciiGlyphMap" in glyph_source
        and "FIXED_GLYPHS.has(glyph)" in glyph_source
    )

    activate_official_environment()
    from mazebench import mazebench as official_environment

    checks["default_start_level"] = (
        official_environment.DEFAULT_START_LEVEL_ID
        == BENCHMARK_DEFAULTS["start_level_id"]
    )
    checks["default_view"] = (
        official_environment.DEFAULT_VIEW == BENCHMARK_DEFAULTS["view"]
    )
    checks["default_yaw"] = (
        official_environment.DEFAULT_YAW == BENCHMARK_DEFAULTS["yaw"]
    )
    checks["fixed_win_threshold"] = (
        official_environment.DEFAULT_GAME_WON_GEM_COUNT
        == BENCHMARK_DEFAULTS["game_won_gem_count"]
    )
    checks["default_reward_weights"] = (
        official_environment.DEFAULT_GEM_REWARD_WEIGHT
        == BENCHMARK_DEFAULTS["gem_reward_weight"]
        and official_environment.DEFAULT_ROOM_REWARD_WEIGHT
        == BENCHMARK_DEFAULTS["room_reward_weight"]
        and official_environment.DEFAULT_PUSH_REWARD_WEIGHT
        == BENCHMARK_DEFAULTS["push_reward_weight"]
    )

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(
            "official MazeBench benchmark contract check failed: "
            + ", ".join(failed)
        )

    delivered = delivered_system_prompt(
        hide_names=hide_names,
        observation_mode=observation_mode,
    )
    hidden_ascii_applied = observation_mode == "ascii" and bool(hide_names)
    return {
        "schema_version": 1,
        "status": "passed",
        "checked_package_version": official["version"],
        "checks": checks,
        "defaults": dict(BENCHMARK_DEFAULTS),
        "selected_observation_mode": observation_mode,
        "selected_hide_names": hide_names,
        "prompt": {
            "base_file": str(prompt_path()),
            "base_sha256": sha256_text(base_system_prompt()),
            "base_unmodified": True,
            "native_agent_prompt_file": str(native_agent_prompt_path()),
            "native_hidden_ascii_instruction": (
                OFFICIAL_HIDDEN_ASCII_INSTRUCTION if hidden_ascii_applied else None
            ),
            "native_hidden_ascii_instruction_applied": hidden_ascii_applied,
            "native_hidden_ascii_instruction_sha256": (
                sha256_text(OFFICIAL_HIDDEN_ASCII_INSTRUCTION)
                if hidden_ascii_applied
                else None
            ),
            "native_observation_instruction": observation_instruction,
            "native_observation_instruction_sha256": sha256_text(
                observation_instruction
            ),
            "observation_mode": observation_mode,
            "hide_names": hide_names,
            "delivered_sha256": sha256_text(delivered),
        },
        "parity_scope": {
            "engine_levels_observations_scoring": "official_pinned_package",
            "base_system_prompt": "official_unmodified",
            "observation_instruction": "official_native_harness_aligned",
            "agent_protocol": "hosted_multiturn_compatibility_not_native_mcp",
            "context_management": "lab_generic_autocompact",
        },
    }


def provenance() -> dict[str, Any]:
    prompt = prompt_path()
    try:
        installed_version = version("mazebench")
    except PackageNotFoundError:
        installed_version = "unknown"
    return {
        "distribution": "mazebench",
        "version": installed_version,
        "expected_version": PINNED_MAZEBENCH_VERSION,
        "runtime": str(official_runtime_root()),
        "environment": str(official_environment_root()),
        "system_prompt_file": str(prompt),
        "system_prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        "system_prompt_file_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        "native_agent_prompt_file": str(native_agent_prompt_path()),
        "glyph_contract_file": str(glyph_contract_path()),
    }
