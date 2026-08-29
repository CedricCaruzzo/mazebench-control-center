#!/usr/bin/env python3
"""Run a local OpenAI-compatible model against official MazeBench ASCII.

The engine, observations, scoring, and prompt are loaded in place from the
pinned official wheel. mblab adds local-model transport and durable journals.

    mazebench-control-center run --actions 256

"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from mblab.compaction import (
    DEFAULT_COMPACT_AT,
    DEFAULT_RECENT_TURNS,
    DEFAULT_SOURCE_COMPACT_AT,
    DEFAULT_TARGET_TOKENS,
    MODE as COMPACTION_MODE,
    NO_COMPACTION_MODE,
    GenericAutoCompactor,
    install_generic_autocompaction,
)
from mblab.interactions import install_openai_interaction_journal
from mblab.forks import (
    install_active_context_fork,
    load_fork_plan,
    write_inherited_journals,
)
from mblab.llamacpp_compat import patch_verifiers_for_llamacpp
from mblab.metrics import derive_metrics
from mblab.official import (
    BENCHMARK_DEFAULTS,
    activate_official_environment,
    benchmark_contract,
    delivered_system_prompt,
    provenance,
)
from mblab.store import RUN_ID, read_json, read_jsonl, write_json

activate_official_environment()

from mazebench import legacy  # noqa: E402
from verifiers.types import ClientConfig  # noqa: E402

def jsonable(obj):
    """Pydantic messages must be dumped, not str()'d, or traces become useless."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    return obj


# The reward components MazeBench exposes per rollout. Always report these
# individually -- the weighted total (gems 1.0 / rooms 0.1 / pushes 0.05) hides
# which of the three actually moved.
COMPONENTS = (
    "reward",
    "gem_score",
    "room_exploration_score",
    "block_progress_score",
    "collected_gems",
    "visited_level_count",
    "block_pushes",
    "novel_block_positions",
    "num_turns",
)


def ensure_official_system_prompt(env, expected_prompt: str) -> bool:
    """Deliver the audited official prompt layers through the legacy adapter.

    verifiers' Environment.__init__ correctly prepends `system_prompt` to every
    dataset row, and the dataset really does carry ['system', 'user']. But
    LegacyMazeEnv.setup_state then does:

        state["prompt"] = [{"role": "user", "content": self.status_prompt(...)}]

    overwriting the list wholesale. The system message never reaches the model.

    This is not cosmetic. The system prompt is the ONLY place that carries:
      - "your entire response must be one exact command line"
      - "Do not include analysis, markdown, JSON, punctuation, or extra words"
      - the existence of `undo` and `reset`
      - what to do when the player has died
      - that moves are screen-relative
    The user message repeats only the movement/camera/quit vocabulary.

    ``expected_prompt`` contains the untouched hosted multiturn base plus the
    official native ASCII identity paragraph when hidden identities are active.
    """
    if not expected_prompt.strip():
        return False

    original = env.setup_state

    async def setup_state(state, **kwargs):
        await original(state, **kwargs)
        prompt = state.get("prompt") or []
        first_role = prompt[0].get("role") if prompt and isinstance(prompt[0], dict) else None
        if first_role == "system":
            if prompt[0].get("content") != expected_prompt:
                raise RuntimeError("unexpected MazeBench system prompt was delivered")
            return
        state["prompt"] = [{"role": "system", "content": expected_prompt}, *prompt]

    env.setup_state = setup_state
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--actions",
        type=int,
        default=256,
        help="action budget for this run or additional actions after a fork",
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    ap.add_argument(
        "--api-key-env",
        default="MAZEBENCH_API_KEY",
        help="environment variable containing the endpoint API key",
    )
    ap.add_argument(
        "--token-count-mode",
        choices=("llama.cpp", "estimate"),
        default="estimate",
    )
    ap.add_argument(
        "--thinking-contract",
        choices=("qwen", "none"),
        default="none",
    )
    ap.add_argument("--model", default="local")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--thinking", choices=("on", "off"), default="off",
                    help="enable model reasoning through llama.cpp's chat template")
    ap.add_argument("--thinking-budget", type=int, default=2048,
                    help="maximum reasoning tokens per action when thinking is on")
    ap.add_argument("--preserve-thinking", choices=("on", "off"), default="off",
                    help="retain prior turns' reasoning in Qwen's rendered model context")
    ap.add_argument(
        "--context-mode",
        choices=(COMPACTION_MODE, NO_COMPACTION_MODE),
        default=COMPACTION_MODE,
        help="working-context policy (raw interaction artifacts remain complete)",
    )
    ap.add_argument("--compact-at-tokens", type=int, default=DEFAULT_COMPACT_AT)
    ap.add_argument(
        "--compact-source-at-tokens",
        type=int,
        default=DEFAULT_SOURCE_COMPACT_AT,
    )
    ap.add_argument(
        "--compaction-summary-budget",
        type=int,
        default=DEFAULT_TARGET_TOKENS,
    )
    ap.add_argument(
        "--compaction-recent-turns", type=int, default=DEFAULT_RECENT_TURNS
    )
    ap.add_argument("--level", default=None, help="start level, e.g. HxI")
    ap.add_argument(
        "--observation-mode",
        choices=("ascii", "json"),
        default="ascii",
        help="official text observation representation",
    )
    ap.add_argument("--hide-names", choices=("on", "off"), default="off",
                    help="randomize ASCII identities instead of using canonical characters")
    ap.add_argument("--hide-names-seed", default="1",
                    help="stable ASCII character-remapping seed")
    ap.add_argument(
        "--system-prompt-mode",
        choices=("official", "unofficial"),
        default="official",
        help="label the exact system prompt as official or an experimental override",
    )
    ap.add_argument(
        "--system-prompt-file",
        default=None,
        help="UTF-8 file containing the exact system-role text to deliver",
    )
    ap.add_argument("--out", default="runs")
    ap.add_argument("--run-id", default=None,
                    help="explicit artifact directory name (used by the control center)")
    ap.add_argument("--fork-parent", default=None,
                    help="completed parent run ID for an active-context branch")
    ap.add_argument("--fork-turn", type=int, default=None,
                    help="parent action/call boundary to reconstruct")
    args = ap.parse_args(argv)

    if args.run_id and not RUN_ID.fullmatch(args.run_id):
        ap.error("--run-id may contain only letters, numbers, dots, dashes, and underscores")
    if bool(args.fork_parent) != (args.fork_turn is not None):
        ap.error("--fork-parent and --fork-turn must be supplied together")
    if args.fork_parent and not RUN_ID.fullmatch(args.fork_parent):
        ap.error("--fork-parent is not a valid run ID")
    args.hide_names_seed = str(args.hide_names_seed).strip()
    if args.hide_names == "on" and not args.hide_names_seed:
        ap.error("--hide-names-seed must not be empty when randomized names are enabled")
    if len(args.hide_names_seed) > 128 or "\x00" in args.hide_names_seed:
        ap.error("--hide-names-seed must be at most 128 characters")
    if not 64 <= args.thinking_budget <= 32768:
        ap.error("--thinking-budget must be between 64 and 32768")
    if args.context_mode == COMPACTION_MODE:
        if not 8_192 <= args.compact_at_tokens <= 75_000:
            ap.error("--compact-at-tokens must be between 8192 and 75000")
        if not 8_192 <= args.compact_source_at_tokens <= 75_000:
            ap.error("--compact-source-at-tokens must be between 8192 and 75000")
        if not 256 <= args.compaction_summary_budget <= 8_192:
            ap.error("--compaction-summary-budget must be between 256 and 8192")
        if not 1 <= args.compaction_recent_turns <= 20:
            ap.error("--compaction-recent-turns must be between 1 and 20")

    # Wire-format only: llama.cpp rejects the explicit nulls verifiers emits.
    patch_verifiers_for_llamacpp()

    hide_names = args.hide_names == "on"
    effective_hide_names_seed = args.hide_names_seed if hide_names else ""
    fork_parent_dir = None
    fork_parent_config: dict[str, object] = {}
    if args.fork_parent:
        runs_root = Path(args.out).resolve()
        fork_parent_dir = (runs_root / args.fork_parent).resolve()
        if fork_parent_dir.parent != runs_root or not fork_parent_dir.is_dir():
            ap.error("fork parent run does not exist")
        fork_parent_manifest = read_json(fork_parent_dir / "run.json", {})
        if fork_parent_manifest.get("status") in {"running", "queued", "stopping"}:
            ap.error("fork parent run is still active")
        fork_parent_config = dict(fork_parent_manifest.get("config") or {})
        parent_hide_names = fork_parent_config.get("hide_names")
        if not isinstance(parent_hide_names, bool):
            ap.error("fork parent does not record its identity condition")
        if args.observation_mode == "ascii" and parent_hide_names != hide_names:
            ap.error("fork must retain the parent's hidden-identity condition")
        if args.observation_mode == "ascii" and hide_names:
            parent_seed = str(fork_parent_config.get("hide_names_seed") or "1")
            if parent_seed != effective_hide_names_seed:
                ap.error("fork must retain the parent's randomized-character seed")
    official = provenance()
    contract = benchmark_contract(
        hide_names=hide_names,
        observation_mode=args.observation_mode,
    )
    official_system_prompt = delivered_system_prompt(
        hide_names=hide_names,
        observation_mode=args.observation_mode,
    )
    run_system_prompt = official_system_prompt
    if args.system_prompt_file:
        prompt_file = Path(args.system_prompt_file)
        if not prompt_file.is_file():
            ap.error("--system-prompt-file does not exist")
        run_system_prompt = prompt_file.read_text(encoding="utf-8")
    elif args.system_prompt_mode == "unofficial":
        ap.error("--system-prompt-file is required for an unofficial prompt")
    if not run_system_prompt.strip():
        ap.error("the selected system prompt must not be empty")
    prompt_matches_official = run_system_prompt == official_system_prompt
    if args.system_prompt_mode == "official" and not prompt_matches_official:
        ap.error("official system-prompt mode requires the exact audited prompt")
    selected_prompt_sha256 = hashlib.sha256(run_system_prompt.encode()).hexdigest()
    contract["prompt"].update(
        {
            "selected_mode": args.system_prompt_mode,
            "selected_sha256": selected_prompt_sha256,
            "selected_matches_official": prompt_matches_official,
        }
    )
    contract["parity_scope"]["selected_system_prompt"] = (
        "official_unmodified"
        if prompt_matches_official
        else "unofficial_user_override"
    )
    parent_level = str(fork_parent_config.get("level") or "").strip()
    if args.level and parent_level and args.level != parent_level:
        ap.error("fork must retain the parent's start level")
    start_level = parent_level or args.level or str(BENCHMARK_DEFAULTS["start_level_id"])
    fork_plan = (
        load_fork_plan(
            fork_parent_dir,
            turn=int(args.fork_turn),
            system_prompt=run_system_prompt,
        )
        if fork_parent_dir is not None and args.fork_turn is not None
        else None
    )
    effective_max_actions = args.actions + (fork_plan.turn if fork_plan else 0)

    # The official hosted compatibility wrapper currently hardcodes seed 1
    # when it constructs its official MazeSession. Substitute only that
    # constructor argument so the native renderer can honor the user-selected
    # stable seed without modifying the installed MazeBench package.
    if hide_names:
        official_session = legacy.MazeSession
        selected_seed = effective_hide_names_seed

        class SeededMazeSession(official_session):
            def __init__(self, *session_args, **session_kwargs):
                session_kwargs["hide_names_seed"] = selected_seed
                super().__init__(*session_args, **session_kwargs)

        SeededMazeSession.__name__ = official_session.__name__
        legacy.MazeSession = SeededMazeSession

    env = legacy.load_environment(
        start_level_id=start_level,
        view=str(BENCHMARK_DEFAULTS["view"]),
        yaw=int(BENCHMARK_DEFAULTS["yaw"]),
        game_won_gem_count=int(BENCHMARK_DEFAULTS["game_won_gem_count"]),
        gem_reward_weight=float(BENCHMARK_DEFAULTS["gem_reward_weight"]),
        room_reward_weight=float(BENCHMARK_DEFAULTS["room_reward_weight"]),
        push_reward_weight=float(BENCHMARK_DEFAULTS["push_reward_weight"]),
        max_actions=effective_max_actions,
        allow_quit=bool(BENCHMARK_DEFAULTS["allow_quit"]),
        auto_quit=bool(BENCHMARK_DEFAULTS["auto_quit"]),
        observation_mode=args.observation_mode,
        omniscient=bool(BENCHMARK_DEFAULTS["omniscient"]),
        hide_names=hide_names,
        target_gems=int(BENCHMARK_DEFAULTS["target_gems"]),
        system_prompt=run_system_prompt,
    )
    if (
        env.observation_mode != args.observation_mode
        or env.hide_names is not hide_names
        or env.max_actions != effective_max_actions
        or env.allow_quit is not True
        or env.auto_quit is not False
    ):
        raise RuntimeError("loaded MazeBench environment failed parity validation")
    prompt_delivered = ensure_official_system_prompt(env, run_system_prompt)
    if not prompt_delivered:
        raise RuntimeError("selected MazeBench system prompt was not delivered")
    if fork_plan:
        install_active_context_fork(env, fork_plan, legacy)

    sampling_args = {
        "temperature": args.temperature,
        # Thinking is returned separately from the exact action when llama.cpp's
        # reasoning parser is active. Keep 128 tokens beyond the requested
        # thinking budget so the model can still emit the exact command.
        "max_tokens": args.thinking_budget + 128 if args.thinking == "on" else 16,
        # Repetition is CORRECT in this task -- ten steps left is "left" ten
        # times. Qwen's recommended presence_penalty=1.5 would punish exactly
        # the right behaviour, so both penalties are pinned to zero.
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "top_p": 0.80,
    }
    if args.thinking_contract == "qwen":
        sampling_args["extra_body"] = {
            "chat_template_kwargs": {
                "enable_thinking": args.thinking == "on",
                "preserve_thinking": (
                    args.thinking == "on" and args.preserve_thinking == "on"
                ),
            },
            "thinking_budget_tokens": args.thinking_budget if args.thinking == "on" else 0,
        }

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        ap.error("--api-key-env must name an environment variable")
    if args.thinking == "on" and args.thinking_contract == "none":
        ap.error("this model profile does not support --thinking on")
    os.environ.setdefault(args.api_key_env, "none")
    client = ClientConfig(
        client_type="openai_chat_completions",
        api_base_url=args.base_url,
        api_key_var=args.api_key_env,
        max_retries=1,
    )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    identity_label = (
        "literal"
        if args.observation_mode == "json"
        else ("random" if hide_names else "canonical")
    )
    ascii_character_mode = (
        ("random" if hide_names else "canonical")
        if args.observation_mode == "ascii"
        else None
    )
    profile = (
        (
            f"unofficial-{args.observation_mode}-prompt-{identity_label}"
        )
        if args.system_prompt_mode == "unofficial"
        else (
            f"official-{args.observation_mode}-{identity_label}-native-contract"
        )
    )
    run_id = args.run_id or f"run-{stamp}-{profile}"
    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    compaction_enabled = args.context_mode == COMPACTION_MODE
    compaction_budget = args.compaction_summary_budget if compaction_enabled else None
    manifest = {
        "schema_version": 5,
        "id": run_id,
        "status": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "actions": args.actions,
            "effective_max_actions": effective_max_actions,
            "base_url": args.base_url,
            "level": start_level,
            "view": BENCHMARK_DEFAULTS["view"],
            "yaw": BENCHMARK_DEFAULTS["yaw"],
            "game_won_gem_count": BENCHMARK_DEFAULTS["game_won_gem_count"],
            "target_gems": BENCHMARK_DEFAULTS["target_gems"],
            "reward_weights": {
                "gems": BENCHMARK_DEFAULTS["gem_reward_weight"],
                "rooms": BENCHMARK_DEFAULTS["room_reward_weight"],
                "pushes": BENCHMARK_DEFAULTS["push_reward_weight"],
            },
            "allow_quit": BENCHMARK_DEFAULTS["allow_quit"],
            "auto_quit": BENCHMARK_DEFAULTS["auto_quit"],
            "omniscient": BENCHMARK_DEFAULTS["omniscient"],
            "model": args.model,
            "api_key_env": args.api_key_env,
            "token_count_mode": args.token_count_mode,
            "thinking_contract": args.thinking_contract,
            "profile": profile,
            "observation_mode": args.observation_mode,
            "ascii_character_mode": ascii_character_mode,
            "hide_names": hide_names,
            "hide_names_seed": effective_hide_names_seed,
            "system_prompt": args.system_prompt_mode,
            "prompt_contract": {
                "base": "official_hosted_multiturn_unmodified",
                "native_observation_instruction": prompt_matches_official,
                "official_delivered_sha256": contract["prompt"]["delivered_sha256"],
                "selected_sha256": selected_prompt_sha256,
                "matches_official": prompt_matches_official,
            },
            "temperature": args.temperature,
            "thinking": args.thinking == "on",
            "thinking_budget": args.thinking_budget if args.thinking == "on" else 0,
            "preserve_thinking": (
                args.thinking == "on" and args.preserve_thinking == "on"
            ),
            "context_management": {
                "mode": args.context_mode,
                "owner": (
                    "control-center"
                    if compaction_enabled
                    else "endpoint-or-upstream-harness"
                ),
                "control_center_compaction": compaction_enabled,
                "compact_at_tokens": args.compact_at_tokens if compaction_enabled else None,
                "compact_source_at_tokens": (
                    args.compact_source_at_tokens if compaction_enabled else None
                ),
                "summary_budget_tokens": compaction_budget,
                "recent_turns_verbatim": (
                    args.compaction_recent_turns if compaction_enabled else None
                ),
                "compactor_model": args.model if compaction_enabled else None,
                "domain_specific_schema": False,
            },
            "fork": (
                {
                    "mode": "active-context",
                    "parent_run_id": fork_plan.parent_run_id,
                    "turn": fork_plan.turn,
                    "inherited_actions": fork_plan.turn,
                    "history_sha256": fork_plan.history_sha256,
                    "checkpoint_board_state_hash": (
                        fork_plan.checkpoint_board_state_hash
                    ),
                }
                if fork_plan
                else None
            ),
        },
        "benchmark": {
            **official,
            "environment_api": "hosted_multiturn_compatibility",
            "engine_and_observations": "official_unmodified",
            "system_prompt_base": (
                "official_unmodified"
                if prompt_matches_official
                else "unofficial_user_override"
            ),
            "system_prompt_layers": (
                "official_hosted_multiturn_plus_official_native_"
                f"{args.observation_mode}_"
                f"{'hidden' if hide_names else 'visible'}_instruction"
                if prompt_matches_official
                else "unofficial_user_supplied"
            ),
            "benchmark_condition": (
                "official_prompt"
                if args.system_prompt_mode == "official"
                else "unofficial_prompt_intervention"
            ),
            "system_prompt_delivery_repair": True,
            "benchmark_contract_status": contract["status"],
            "benchmark_contract_artifact": "benchmark-contract.json",
            "native_mcp_harness_equivalent": False,
            "local_model_transport": "openai_chat_completions",
            "context_management": (
                "generic_model_generated_autocompaction"
                if compaction_enabled
                else "endpoint_managed_or_full_history"
            ),
        },
    }
    write_json(out_dir / "benchmark-contract.json", contract)
    (out_dir / "system-prompt.txt").write_text(run_system_prompt, encoding="utf-8")
    write_json(out_dir / "run.json", manifest)

    if fork_plan:
        write_json(
            out_dir / "fork.json",
            {
                "schema_version": 1,
                **manifest["config"]["fork"],
                "parent_path": str(fork_plan.parent_dir),
                "history_mode": "latest_model_visible_context",
                "active_context_source_call": fork_plan.turn,
                "history_messages": len(fork_plan.history),
                "inherited_interactions": len(fork_plan.interactions),
                "inherited_compaction_events": len(fork_plan.compactions),
                "verification": "per_action_board_state_hash",
            },
        )

    # Capture the native response before MazeBench projects the in-memory
    # trajectory down to environment-only completion messages. This retains
    # reasoning_content, final content, usage, timings, and request failures.
    live_interactions_path = out_dir / "interactions.jsonl"
    live_compactions_path = out_dir / "compactions.jsonl"
    live_actions_path = out_dir / "actions.jsonl"
    # Keep the run-artifact schema stable when Control Center compaction is off.
    # In that mode this remains empty unless a fork inherited earlier events.
    live_compactions_path.touch(exist_ok=True)
    if fork_plan:
        write_inherited_journals(
            fork_plan,
            actions_path=live_actions_path,
            interactions_path=live_interactions_path,
            compactions_path=live_compactions_path,
        )
    install_openai_interaction_journal(
        live_interactions_path,
        call_offset=fork_plan.turn if fork_plan else 0,
    )
    compactor = None
    if compaction_enabled:
        compactor = GenericAutoCompactor(
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            token_count_mode=args.token_count_mode,
            thinking_contract=args.thinking_contract,
            journal_path=live_compactions_path,
            compact_at_tokens=args.compact_at_tokens,
            source_compact_at_tokens=args.compact_source_at_tokens,
            summary_budget_tokens=args.compaction_summary_budget,
            recent_turns=args.compaction_recent_turns,
            call_offset=fork_plan.turn if fork_plan else 0,
            inherited_assistant_count=(
                sum(
                    1
                    for message in fork_plan.history
                    if message.get("role") == "assistant"
                )
                if fork_plan
                else 0
            ),
        )
        # Installed after the interaction journal: the outer compactor transforms
        # the prompt first, so interactions.jsonl records exactly what the model saw.
        install_generic_autocompaction(compactor)

    context_label = (
        f"{args.context_mode} at {args.compact_source_at_tokens:,} source / "
        f"{args.compact_at_tokens:,} working"
        if compaction_enabled
        else "endpoint-managed / no Control Center compaction"
    )
    print(f"budget {args.actions} actions | temp {args.temperature} | "
          f"thinking: {args.thinking} ({args.thinking_budget} token budget, "
          f"history {args.preserve_thinking}) | "
          f"context: {context_label} | "
          f"profile: {profile} | {args.system_prompt_mode} prompt delivered -> "
          f"{out_dir}")

    # Stream each completed engine interaction to disk. The final actions.json
    # remains canonical, while this append-only journal lets the control center
    # follow a rollout before evaluate_sync returns.
    original_record_action = legacy.record_maze_action

    def record_and_stream(state, **kwargs):  # type: ignore[no-untyped-def]
        original_record_action(state, **kwargs)
        actions = state.get("maze_actions", []) if isinstance(state, dict) else state.maze_actions
        if actions:
            # Evaluator-private renderer state is never sent to the model. Keep
            # it only in the owner-side trace so the official WebGL viewer can
            # reconstruct an exact read-only frame without altering the game.
            render_state = (kwargs.get("status") or {}).get("_render_state")
            if isinstance(render_state, dict):
                actions[-1]["render_state"] = jsonable(render_state)
            with live_actions_path.open("a") as stream:
                stream.write(json.dumps(jsonable(actions[-1]), default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    legacy.record_maze_action = record_and_stream

    t0 = time.perf_counter()
    results = env.evaluate_sync(
        client=client,
        model=args.model,
        sampling_args=sampling_args,
        num_examples=1,
        rollouts_per_example=1,
        max_concurrent=1,
        # The model's own actions are NOT in `completion` -- that holds only the
        # env's observations. The action trace lives in env state, and state is
        # dropped from the result unless it is requested by name.
        state_columns=["maze_actions", "maze_conversation_log", "maze_scorecard", "maze_replay"],
    )
    elapsed = time.perf_counter() - t0

    rollout = results["outputs"][0]
    actions = rollout.get("maze_actions") or []
    inherited_action_count = fork_plan.turn if fork_plan else 0
    completed_actions = max(0, len(actions) - inherited_action_count)
    if not completed_actions and not fork_plan:
        completed_actions = int(rollout.get("num_turns") or 0)
    seconds_per_action = (
        elapsed / completed_actions if completed_actions else None
    )

    timing = (
        f"{seconds_per_action:.2f}s per completed action"
        if seconds_per_action is not None
        else "no completed actions"
    )
    print(f"\nwall {elapsed:.1f}s  ({timing})")
    for key in COMPONENTS:
        if key in rollout:
            print(f"  {key:24s} {rollout[key]}")
    if rollout.get("error"):
        print(f"  ERROR: {rollout['error']}")
    print(f"  stop_condition           {rollout.get('stop_condition')}")

    compaction_records = read_jsonl(live_compactions_path)
    compaction_input_tokens = 0
    compaction_output_tokens = 0
    compaction_cached_tokens = 0
    for record in compaction_records:
        if record.get("inherited_from"):
            continue
        usage = record.get("response_usage") or {}
        compaction_input_tokens += int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        )
        compaction_output_tokens += int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        details = usage.get("prompt_tokens_details") or {}
        compaction_cached_tokens += int(details.get("cached_tokens") or 0)
    main_token_usage = jsonable(rollout.get("token_usage") or {})
    main_input_tokens = int(main_token_usage.get("input_tokens") or 0)
    main_output_tokens = int(main_token_usage.get("output_tokens") or 0)
    token_usage = {
        **main_token_usage,
        "main_input_tokens": main_input_tokens,
        "main_output_tokens": main_output_tokens,
        "compaction_input_tokens": compaction_input_tokens,
        "compaction_output_tokens": compaction_output_tokens,
        "compaction_cached_input_tokens": compaction_cached_tokens,
        "input_tokens": main_input_tokens + compaction_input_tokens,
        "output_tokens": main_output_tokens + compaction_output_tokens,
    }

    summary = {
        "actions_budget": args.actions,
        "effective_max_actions": effective_max_actions,
        "inherited_actions": inherited_action_count,
        "branch_actions": completed_actions,
        "profile": profile,
        "observation_mode": args.observation_mode,
        "ascii_character_mode": ascii_character_mode,
        "hide_names": hide_names,
        "hide_names_seed": effective_hide_names_seed,
        "system_prompt": args.system_prompt_mode,
        "base_system_prompt_sha256": contract["prompt"]["base_sha256"],
        "base_system_prompt_file_sha256": official["system_prompt_file_sha256"],
        "official_system_prompt_sha256": contract["prompt"]["delivered_sha256"],
        "system_prompt_sha256": selected_prompt_sha256,
        "system_prompt_matches_official": prompt_matches_official,
        "native_observation_instruction": prompt_matches_official,
        "native_ascii_identity_instruction": (
            args.observation_mode == "ascii"
            and hide_names
            and prompt_matches_official
        ),
        "system_prompt_actually_sent": prompt_delivered,
        "thinking": args.thinking == "on",
        "thinking_budget": args.thinking_budget if args.thinking == "on" else 0,
        "preserve_thinking": (
            args.thinking == "on" and args.preserve_thinking == "on"
        ),
        "context_management": {
            **manifest["config"]["context_management"],
            "compactions": compactor.compaction_count if compactor else 0,
        },
        "elapsed_s": round(elapsed, 2),
        "s_per_action": (
            round(seconds_per_action, 3)
            if seconds_per_action is not None
            else None
        ),
        "components": {k: rollout.get(k) for k in COMPONENTS if k in rollout},
        "stop_condition": rollout.get("stop_condition"),
        "is_truncated": rollout.get("is_truncated"),
        "error": rollout.get("error"),
        "token_usage": token_usage,
        "sampling_args": sampling_args,
    }
    write_json(out_dir / "summary.json", summary)

    # The trace is the actual deliverable of phase 1.
    (out_dir / "prompt.json").write_text(
        json.dumps(jsonable(rollout.get("prompt")), indent=2, default=str))
    (out_dir / "completion.json").write_text(
        json.dumps(jsonable(rollout.get("completion")), indent=2, default=str))
    (out_dir / "metadata.json").write_text(json.dumps(results.get("metadata"), indent=2, default=str))

    # Write a rollout.jsonl in the shape the ENGINE's own replay exporter reads
    # (it looks for `maze_replay` + `maze_actions` at row top level or under
    # `info`). That means 3D replay comes for free -- no renderer to build.
    (out_dir / "rollout.jsonl").write_text(json.dumps({
        "maze_replay": jsonable(rollout.get("maze_replay")),
        "maze_actions": jsonable(rollout.get("maze_actions")),
        "maze_scorecard": jsonable(rollout.get("maze_scorecard")),
        "info": {
            "model": args.model,
            "profile": profile,
            "mazebench_version": official["version"],
            "base_system_prompt_sha256": contract["prompt"]["base_sha256"],
            "base_system_prompt_file_sha256": official["system_prompt_file_sha256"],
            "official_system_prompt_sha256": contract["prompt"]["delivered_sha256"],
            "system_prompt_sha256": selected_prompt_sha256,
            "system_prompt_matches_official": prompt_matches_official,
        },
        **{k: rollout.get(k) for k in COMPONENTS if k in rollout},
    }, default=str) + "\n")

    if actions:
        write_json(out_dir / "actions.json", jsonable(actions))
        cmds = [a.get("command", a) if isinstance(a, dict) else a for a in actions]
        print(f"  actions                  {cmds}")

    write_json(
        out_dir / "metrics.json",
        derive_metrics(jsonable(actions), summary, results.get("metadata") or {}),
    )
    # JSONL is durable during the rollout. The array is a convenient immutable
    # snapshot for completed runs; readers fall back to JSONL after interruption.
    write_json(out_dir / "interactions.json", read_jsonl(live_interactions_path))
    write_json(
        out_dir / "compactions.json",
        compaction_records,
    )
    manifest.update(
        {
            "status": "failed" if rollout.get("error") else "completed",
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "error": rollout.get("error"),
            "artifacts": [
                "actions.json",
                "actions.jsonl",
                "benchmark-contract.json",
                "completion.json",
                "compactions.json",
                "compactions.jsonl",
                "interactions.json",
                "interactions.jsonl",
                "metadata.json",
                "metrics.json",
                "prompt.json",
                "rollout.jsonl",
                "summary.json",
                "system-prompt.txt",
                *(["fork.json"] if fork_plan else []),
            ],
        }
    )
    write_json(out_dir / "run.json", manifest)

    print(f"\nwrote {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
