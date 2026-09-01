"""Derived MazeBench metrics.

Raw actions are the source of truth.  Everything in this module is intentionally
recomputable so adding a chart later never requires migrating historical runs.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import log2
from typing import Any, Iterable


NOVELTY_WINDOW = 25
BEHAVIOR_WINDOW = 25
ACTION_VOCABULARY_SIZE = 10


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _component(summary: dict[str, Any], name: str, default: Any = 0) -> Any:
    return (summary.get("components") or {}).get(name, default)


def _entropy(values: Iterable[str]) -> float:
    rows = list(values)
    if not rows:
        return 0.0
    counts = Counter(rows)
    return -sum(
        (count / len(rows)) * log2(count / len(rows)) for count in counts.values()
    )


def derive_metrics(
    actions: Iterable[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return summary, timeline, room, and command metrics for one rollout."""
    action_list = list(actions)
    summary = summary or {}
    metadata = metadata or {}
    usage = summary.get("token_usage") or metadata.get("usage") or {}

    seen_hashes: set[str] = set()
    seen_rooms: set[str] = set()
    novelty_bits: list[int] = []
    timeline: list[dict[str, Any]] = []
    command_counts: Counter[str] = Counter()
    behavior_command_counts: Counter[str] = Counter()
    behavior_commands: list[str] = []
    failure_bits: list[int] = []
    room_counts: Counter[str] = Counter()
    first_room_turn: dict[str, int] = {}
    room_entries: Counter[str] = Counter()
    longest_plateau = 0
    plateau = 0
    start_seconds = _iso_seconds(action_list[0].get("timestamp")) if action_list else None
    previous_room = ""
    previous_gems = 0
    longest_command_streak = 0
    command_streak = 0
    longest_failed_streak = 0
    failed_streak = 0
    action_switches = 0
    oscillations = 0
    first_gem_action: int | None = None
    first_room_transition_action: int | None = None

    for index, action in enumerate(action_list, start=1):
        status = action.get("status") or {}
        turn = int(_number(action.get("turn"), index))
        state_hash = str(status.get("board_state_hash") or "")
        novel = int(bool(state_hash and state_hash not in seen_hashes))
        if state_hash:
            seen_hashes.add(state_hash)
        novelty_bits.append(novel)
        plateau = 0 if novel else plateau + 1
        longest_plateau = max(longest_plateau, plateau)

        room = str(status.get("current_room") or "unknown")
        room_counts[room] += 1
        if room not in seen_rooms:
            seen_rooms.add(room)
            first_room_turn[room] = turn
        entered_room = room != previous_room
        if entered_room:
            room_entries[room] += 1
            previous_room = room

        command = str(action.get("command") or action.get("raw_response") or "unknown")
        normalized = str(action.get("normalized_action") or status.get("action") or command)
        command_counts[normalized] += 1
        behavior_command = command.strip().lower() or "unknown"
        if behavior_commands and behavior_command != behavior_commands[-1]:
            action_switches += 1
        command_streak = (
            command_streak + 1
            if behavior_commands and behavior_command == behavior_commands[-1]
            else 1
        )
        longest_command_streak = max(longest_command_streak, command_streak)
        behavior_commands.append(behavior_command)
        behavior_command_counts[behavior_command] += 1
        if (
            len(behavior_commands) >= 4
            and behavior_commands[-1] == behavior_commands[-3]
            and behavior_commands[-2] == behavior_commands[-4]
            and behavior_commands[-1] != behavior_commands[-2]
        ):
            oscillations += 1
        failed_action = status.get("moved") is False
        failure_bits.append(int(failed_action))
        failed_streak = failed_streak + 1 if failed_action else 0
        longest_failed_streak = max(longest_failed_streak, failed_streak)
        gems = int(_number(status.get("gem_count"), len(status.get("collected_gems") or [])))
        if first_gem_action is None and gems > previous_gems:
            first_gem_action = turn
        if first_room_transition_action is None and len(seen_rooms) > 1 and entered_room:
            first_room_transition_action = turn
        player = status.get("player") or {}
        timestamp_seconds = _iso_seconds(action.get("timestamp"))
        window = novelty_bits[-NOVELTY_WINDOW:]
        behavior_window = behavior_commands[-BEHAVIOR_WINDOW:]
        behavior_entropy_bits = _entropy(behavior_window)
        failure_window = failure_bits[-BEHAVIOR_WINDOW:]
        player_dead = bool(status.get("player_dead"))
        is_reset = normalized.strip().lower() == "reset" or command.strip().lower() == "reset"
        path_break_reason = (
            "death"
            if player_dead
            else "reset"
            if is_reset
            else "room-entry"
            if entered_room
            else None
        )
        timeline.append(
            {
                "turn": turn,
                "inherited": bool(action.get("inherited")),
                "elapsed_s": round(timestamp_seconds - start_seconds, 3)
                if timestamp_seconds is not None and start_seconds is not None
                else None,
                "timestamp": action.get("timestamp"),
                "command": command,
                "action": normalized,
                "valid": bool(action.get("valid", True)),
                "moved": status.get("moved"),
                "room": room,
                "room_changed": bool(status.get("room_changed")),
                "rooms_visited": len(status.get("visited_levels") or seen_rooms),
                "gems": gems,
                "gem_delta": max(0, gems - previous_gems),
                "unique_states": len(seen_hashes),
                "novel_state": bool(novel),
                "novelty_rolling": round(sum(window) / len(window), 4),
                "action_entropy_rolling_bits": round(behavior_entropy_bits, 4),
                "action_entropy_rolling": round(
                    min(1.0, behavior_entropy_bits / log2(ACTION_VOCABULARY_SIZE)),
                    4,
                ),
                "failed_action": failed_action,
                "failed_action_rolling": round(
                    sum(failure_window) / len(failure_window), 4
                ),
                "command_streak": command_streak,
                "oscillation": bool(
                    len(behavior_commands) >= 4
                    and behavior_commands[-1] == behavior_commands[-3]
                    and behavior_commands[-2] == behavior_commands[-4]
                    and behavior_commands[-1] != behavior_commands[-2]
                ),
                "plateau_length": plateau,
                "pushes": int(_number(status.get("push_count"))),
                "player_dead": player_dead,
                # Positions are post-action. A death/reset position is therefore
                # a respawn point, not a movement destination; path charts must
                # start a new segment rather than draw a teleport across the room.
                "path_break_reason": path_break_reason,
                "x": player.get("x"),
                "y": player.get("y"),
                "elevation": player.get("elevation"),
                "view": status.get("current_view"),
                "yaw": status.get("yaw"),
                "board": status.get("level") or "",
            }
        )
        previous_gems = gems

    final = (action_list[-1].get("status") or {}) if action_list else {}
    turns = len(action_list)
    unique_states = len(seen_hashes)
    rooms_visited = int(
        _number(
            _component(summary, "visited_level_count", None),
            len(final.get("visited_levels") or seen_rooms),
        )
    )
    gems = int(
        _number(
            _component(summary, "collected_gems", None),
            final.get("gem_count", len(final.get("collected_gems") or [])),
        )
    )
    input_tokens = int(_number(usage.get("input_tokens")))
    output_tokens = int(_number(usage.get("output_tokens")))
    total_tokens = input_tokens + output_tokens
    token_action_count = (
        int(_number(summary.get("branch_actions")))
        if "branch_actions" in summary
        else turns
    )
    movement_attempts = sum(
        1 for action in action_list if (action.get("normalized_action") or "") == "move"
    )
    moved = sum(1 for action in action_list if (action.get("status") or {}).get("moved"))
    failed_actions = sum(failure_bits)
    behavior_entropy_bits = _entropy(behavior_commands)

    room_rows = [
        {
            "room": room,
            "first_turn": first_room_turn[room],
            "actions": room_counts[room],
            "entries": room_entries[room],
            "share": round(room_counts[room] / max(1, turns), 4),
        }
        for room in sorted(seen_rooms, key=lambda item: first_room_turn[item])
    ]

    return {
        "schema_version": 1,
        "novelty_window": NOVELTY_WINDOW,
        "summary": {
            "actions": turns,
            "inherited_actions": int(_number(summary.get("inherited_actions"))),
            "branch_actions": token_action_count,
            "valid_actions": sum(bool(action.get("valid", True)) for action in action_list),
            "rooms_visited": rooms_visited,
            "gems": gems,
            "pushes": int(_number(_component(summary, "block_pushes"), final.get("push_count"))),
            "unique_states": unique_states,
            "revisited_states": max(0, turns - unique_states),
            "novelty_rate": round(unique_states / max(1, turns), 4),
            "longest_plateau": longest_plateau,
            "current_plateau": plateau,
            "deaths": sum(bool((action.get("status") or {}).get("player_dead")) for action in action_list),
            "movement_success_rate": round(moved / max(1, movement_attempts), 4),
            "action_entropy_bits": round(behavior_entropy_bits, 4),
            "action_entropy_normalized": round(
                min(1.0, behavior_entropy_bits / log2(ACTION_VOCABULARY_SIZE)),
                4,
            ),
            "action_switches": action_switches,
            "action_switch_rate": round(action_switches / max(1, turns - 1), 4),
            "longest_command_streak": longest_command_streak,
            "oscillations": oscillations,
            "oscillations_per_100_actions": round(
                oscillations * 100 / max(1, turns), 2
            ),
            "failed_actions": failed_actions,
            "failed_action_rate": round(failed_actions / max(1, turns), 4),
            "longest_failed_action_streak": longest_failed_streak,
            "revisited_state_rate": round(
                max(0, turns - unique_states) / max(1, turns), 4
            ),
            "longest_plateau_share": round(
                longest_plateau / max(1, turns), 4
            ),
            "unique_states_per_100_actions": round(
                unique_states * 100 / max(1, turns), 2
            ),
            "rooms_per_100_actions": round(
                rooms_visited * 100 / max(1, turns), 2
            ),
            "gems_per_100_actions": round(gems * 100 / max(1, turns), 2),
            "first_gem_action": first_gem_action,
            "first_room_transition_action": first_room_transition_action,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "tokens_per_action": (
                round(total_tokens / token_action_count, 1)
                if token_action_count
                else None
            ),
            "tokens_per_unique_state": round(total_tokens / unique_states, 1)
            if unique_states
            else None,
            "elapsed_s": _number(summary.get("elapsed_s"), metadata.get("time")),
        },
        "commands": dict(command_counts.most_common()),
        "behavior_commands": dict(behavior_command_counts.most_common()),
        "rooms": room_rows,
        "timeline": timeline,
    }
