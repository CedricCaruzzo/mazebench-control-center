import json
import tempfile
import unittest
from pathlib import Path

from mblab.forks import (
    active_context_history,
    load_fork_plan,
    recontextualized_history,
)


def interaction(call, prompt, response):
    return {
        "call": call,
        "request": {
            "message_count": len(prompt),
            "shared_prefix_messages": 0,
            "appended_messages": prompt,
        },
        "response": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": response,
                        "reasoning_content": f"reasoning-{call}",
                    },
                }
            ]
        },
    }


class ForkPlanTest(unittest.TestCase):
    def test_rebuilds_raw_history_across_a_parent_compaction(self):
        system = {"role": "system", "content": "old system"}
        first = [system, {"role": "user", "content": "observation-1"}]
        second = [
            *first,
            {"role": "assistant", "content": "up"},
            {"role": "user", "content": "observation-2"},
        ]
        compacted = [
            system,
            {"role": "user", "content": "compaction summary"},
            {"role": "assistant", "content": "right"},
            {"role": "user", "content": "observation-3"},
        ]
        records = [
            interaction(1, first, "up"),
            interaction(2, second, "right"),
            interaction(3, compacted, "down"),
        ]
        history = recontextualized_history(
            records, through_call=3, system_prompt="new audited system"
        )
        self.assertEqual(history[0]["content"], "new audited system")
        self.assertEqual(
            [message["content"] for message in history[1:]],
            [
                "observation-1",
                "up",
                "observation-2",
                "right",
                "observation-3",
                "down",
            ],
        )
        self.assertEqual(history[-1]["reasoning_content"], "reasoning-3")

    def test_active_context_keeps_latest_summary_without_expanding_raw_history(self):
        system = {"role": "system", "content": "old system"}
        first = [system, {"role": "user", "content": "old observation"}]
        compacted = [
            system,
            {"role": "user", "content": "latest compaction summary"},
            {"role": "assistant", "content": "left"},
            {"role": "user", "content": "recent observation"},
        ]
        records = [
            interaction(1, first, "up"),
            interaction(2, compacted, "right"),
        ]

        history = active_context_history(
            records, through_call=2, system_prompt="new audited system"
        )

        self.assertEqual(history[0]["content"], "new audited system")
        self.assertEqual(
            [message["content"] for message in history[1:]],
            ["latest compaction summary", "left", "recent observation", "right"],
        )
        self.assertNotIn("old observation", json.dumps(history))

    def test_load_plan_requires_and_records_verifiable_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "run-parent"
            parent.mkdir()
            (parent / "run.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "config": {"level": "level_HxI", "hide_names": True},
                    }
                )
            )
            action = {
                "turn": 1,
                "timestamp": "2026-01-01T00:00:02Z",
                "valid": True,
                "command": "up",
                "status": {"board_state_hash": "verified-hash"},
            }
            (parent / "actions.jsonl").write_text(json.dumps(action) + "\n")
            prompt = [
                {"role": "system", "content": "old"},
                {"role": "user", "content": "observation"},
            ]
            (parent / "interactions.jsonl").write_text(
                json.dumps(interaction(1, prompt, "up")) + "\n"
            )
            plan = load_fork_plan(
                parent, turn=1, system_prompt="new audited system"
            )
            self.assertEqual(plan.turn, 1)
            self.assertEqual(plan.checkpoint_board_state_hash, "verified-hash")
            self.assertEqual(plan.history[0]["content"], "new audited system")
            self.assertEqual(plan.history[-1]["content"], "up")
            self.assertTrue(plan.history_sha256)


if __name__ == "__main__":
    unittest.main()
