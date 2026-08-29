"""Verified branches from recorded MazeBench decisions and active context."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mblab.compaction import digest
from mblab.store import read_json, read_jsonl


@dataclass(frozen=True)
class ForkPlan:
    parent_run_id: str
    turn: int
    parent_dir: Path
    actions: list[dict[str, Any]]
    interactions: list[dict[str, Any]]
    compactions: list[dict[str, Any]]
    history: list[dict[str, Any]]
    parent_config: dict[str, Any]
    checkpoint_board_state_hash: str

    @property
    def history_sha256(self) -> str:
        return digest(self.history)


def _request_prompts(
    records: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Reconstruct every exact model-visible request from journal deltas."""
    prompts: dict[int, list[dict[str, Any]]] = {}
    previous: list[dict[str, Any]] = []
    for record in records:
        try:
            call = int(record.get("call"))
        except (TypeError, ValueError):
            continue
        request = record.get("request") or {}
        try:
            shared = int(request.get("shared_prefix_messages") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"parent call {call} has an invalid prompt delta") from None
        appended = request.get("appended_messages") or []
        if not isinstance(appended, list) or shared < 0 or shared > len(previous):
            raise ValueError(f"parent call {call} has an invalid prompt delta")
        current = copy.deepcopy(previous[:shared] + appended)
        expected_count = request.get("message_count")
        if expected_count is not None and len(current) != int(expected_count):
            raise ValueError(f"parent call {call} prompt delta does not reconstruct")
        prompts[call] = current
        previous = current
    return prompts


def recontextualized_history(
    records: list[dict[str, Any]],
    *,
    through_call: int,
    system_prompt: str,
) -> list[dict[str, Any]]:
    """Build a lossless raw dialogue, ignoring prior compaction boundaries.

    Each model call contributes the observation that immediately preceded it
    and the native assistant response. This recovers old turns even when a
    later request replaced them with a compaction summary.
    """
    prompts = _request_prompts(records)
    by_call: dict[int, dict[str, Any]] = {}
    for record in records:
        try:
            by_call[int(record.get("call"))] = record
        except (TypeError, ValueError):
            continue

    history: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]
    for call in range(1, through_call + 1):
        prompt = prompts.get(call)
        record = by_call.get(call)
        if not prompt or not record:
            raise ValueError(f"parent interaction journal is missing call {call}")
        user_message = next(
            (
                message
                for message in reversed(prompt)
                if isinstance(message, dict) and message.get("role") == "user"
            ),
            None,
        )
        if user_message is None:
            raise ValueError(f"parent call {call} has no user observation")
        choices = (record.get("response") or {}).get("choices") or []
        assistant = (choices[0].get("message") or {}) if choices else {}
        if not isinstance(assistant, dict) or not assistant:
            raise ValueError(f"parent call {call} has no completed assistant response")
        assistant = copy.deepcopy(assistant)
        assistant["role"] = "assistant"
        history.extend((copy.deepcopy(user_message), assistant))
    return history


def active_context_history(
    records: list[dict[str, Any]],
    *,
    through_call: int,
    system_prompt: str,
) -> list[dict[str, Any]]:
    """Restore the bounded context active immediately after one model call.

    The interaction journal stores the exact prompt delivered to the model for
    every call, after automatic compaction.  A continuation therefore needs
    only that prompt plus the call's assistant response.  Older raw messages
    represented by a compaction summary stay in the audit journal and must not
    be expanded back into the child's working context.
    """
    prompts = _request_prompts(records)
    prompt = copy.deepcopy(prompts.get(through_call) or [])
    if not prompt:
        raise ValueError(
            f"parent interaction journal is missing call {through_call}"
        )
    if prompt[0].get("role") != "system":
        raise ValueError(f"parent call {through_call} has no system message")
    prompt[0] = {**prompt[0], "content": system_prompt}

    record = next(
        (
            item
            for item in records
            if str(item.get("call")) == str(through_call)
        ),
        None,
    )
    choices = ((record or {}).get("response") or {}).get("choices") or []
    assistant = (choices[0].get("message") or {}) if choices else {}
    if not isinstance(assistant, dict) or not assistant:
        raise ValueError(
            f"parent call {through_call} has no completed assistant response"
        )
    assistant = copy.deepcopy(assistant)
    assistant["role"] = "assistant"
    prompt.append(assistant)
    return prompt


def _completed_compactions_before(
    records: list[dict[str, Any]],
    *,
    turn: int,
    cutoff_timestamp: str,
) -> list[dict[str, Any]]:
    selected = []
    for record in records:
        if not isinstance(record, dict):
            continue
        before_call = record.get("before_call")
        if before_call is not None:
            try:
                if int(before_call) > turn:
                    continue
            except (TypeError, ValueError):
                continue
        elif str(record.get("started_at") or "") > cutoff_timestamp:
            continue
        selected.append(copy.deepcopy(record))
    return selected


def load_fork_plan(
    parent_dir: Path,
    *,
    turn: int,
    system_prompt: str,
) -> ForkPlan:
    if turn < 1:
        raise ValueError("fork turn must be at least 1")
    manifest = read_json(parent_dir / "run.json", {})
    actions = read_json(parent_dir / "actions.json", None)
    if not isinstance(actions, list):
        actions = read_jsonl(parent_dir / "actions.jsonl")
    interactions = read_json(parent_dir / "interactions.json", None)
    if not isinstance(interactions, list):
        interactions = read_jsonl(parent_dir / "interactions.jsonl")
    actions = [item for item in actions if isinstance(item, dict)]
    interactions = [item for item in interactions if isinstance(item, dict)]
    if turn > len(actions):
        raise ValueError(
            f"fork turn {turn} exceeds the parent's {len(actions)} recorded actions"
        )
    prefix = copy.deepcopy(actions[:turn])
    for expected_turn, action in enumerate(prefix, start=1):
        if int(action.get("turn") or 0) != expected_turn:
            raise ValueError(f"parent action journal is missing turn {expected_turn}")
        board_hash = str((action.get("status") or {}).get("board_state_hash") or "")
        if not board_hash:
            raise ValueError(
                f"parent action {expected_turn} has no verifiable board-state hash"
            )
    history = active_context_history(
        interactions,
        through_call=turn,
        system_prompt=system_prompt,
    )
    compactions = read_json(parent_dir / "compactions.json", None)
    if not isinstance(compactions, list):
        compactions = read_jsonl(parent_dir / "compactions.jsonl")
    cutoff = str(prefix[-1].get("timestamp") or "")
    return ForkPlan(
        parent_run_id=parent_dir.name,
        turn=turn,
        parent_dir=parent_dir,
        actions=prefix,
        interactions=copy.deepcopy(interactions[:turn]),
        compactions=_completed_compactions_before(
            [item for item in compactions if isinstance(item, dict)],
            turn=turn,
            cutoff_timestamp=cutoff,
        ),
        history=history,
        parent_config=dict(manifest.get("config") or {}),
        checkpoint_board_state_hash=str(
            (prefix[-1].get("status") or {}).get("board_state_hash") or ""
        ),
    )


def write_inherited_journals(
    plan: ForkPlan,
    *,
    actions_path: Path,
    interactions_path: Path,
    compactions_path: Path,
) -> None:
    action_lines = []
    for record in plan.actions:
        inherited = copy.deepcopy(record)
        inherited["inherited"] = True
        inherited["inherited_from_run"] = plan.parent_run_id
        action_lines.append(json.dumps(inherited, separators=(",", ":"), default=str))
    actions_path.write_text(
        "\n".join(action_lines) + ("\n" if action_lines else ""),
        encoding="utf-8",
    )

    interaction_lines = []
    for record in plan.interactions:
        inherited = copy.deepcopy(record)
        inherited["inherited_from"] = {
            "run_id": plan.parent_run_id,
            "turn": int(record.get("call") or 0),
        }
        interaction_lines.append(json.dumps(inherited, separators=(",", ":"), default=str))
    interactions_path.write_text(
        "\n".join(interaction_lines) + ("\n" if interaction_lines else ""),
        encoding="utf-8",
    )

    compaction_lines = []
    for record in plan.compactions:
        inherited = copy.deepcopy(record)
        inherited["inherited_from"] = {
            "run_id": plan.parent_run_id,
            "fork_turn": plan.turn,
        }
        compaction_lines.append(json.dumps(inherited, separators=(",", ":"), default=str))
    compactions_path.write_text(
        "\n".join(compaction_lines) + ("\n" if compaction_lines else ""),
        encoding="utf-8",
    )


def install_active_context_fork(env: Any, plan: ForkPlan, legacy: Any) -> None:
    """Replay and verify the engine, then replace prompt history for call N+1."""
    original = env.setup_state

    async def setup_state(state: Any, **kwargs: Any) -> None:
        await original(state, **kwargs)
        session = state.get("maze_session")
        if session is None:
            raise RuntimeError("MazeBench fork could not access the maze session")
        status = state.get("maze_status") or {}
        inherited_actions: list[dict[str, Any]] = []
        for expected_turn, saved in enumerate(plan.actions, start=1):
            raw_response = str(saved.get("command") or saved.get("raw_response") or "")
            if saved.get("valid", True) is False or saved.get("error"):
                status = legacy.apply_quit_policy(
                    session.request("observe"), env.allow_quit
                )
            else:
                command, action_args = legacy.parse_text_action(raw_response)
                status = legacy.apply_quit_policy(
                    session.request(command, **action_args), env.allow_quit
                )
            expected_hash = str(
                (saved.get("status") or {}).get("board_state_hash") or ""
            )
            actual_hash = str(status.get("board_state_hash") or "")
            if actual_hash != expected_hash:
                raise ValueError(
                    "fork replay diverged at parent turn "
                    f"{expected_turn}: expected {expected_hash}, got {actual_hash}"
                )
            inherited = copy.deepcopy(saved)
            inherited["inherited"] = True
            inherited["inherited_from_run"] = plan.parent_run_id
            inherited_actions.append(inherited)

        state["maze_status"] = status
        state["maze_actions"] = inherited_actions
        replay = dict(state.get("maze_replay") or {})
        replay["actions"] = inherited_actions
        state["maze_replay"] = replay
        state["game_lost"] = bool(
            env.allow_quit and (status.get("game_lost") or status.get("quit"))
        )
        state["game_won"] = int(status.get("gem_count") or 0) >= 100
        if state["game_lost"] or state["game_won"]:
            raise ValueError("cannot fork from a terminal MazeBench state")

        last = plan.actions[-1]
        result_text = legacy.action_result_text(
            command=str(last.get("normalized_action") or "") or None,
            error=str(last.get("error") or "") or None,
            status=status,
        )
        row = state.get("maze_row") or {}
        current_user = {
            "role": "user",
            "content": env.status_prompt(row, status, result_text),
        }
        state["prompt"] = copy.deepcopy(plan.history) + [current_user]

    env.setup_state = setup_state
