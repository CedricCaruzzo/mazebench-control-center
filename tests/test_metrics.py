import unittest
import json
import tempfile
from pathlib import Path

from mblab.metrics import derive_metrics
from mblab.store import RunStore


def action(turn, room, state_hash, gems=0, moved=True):
    return {
        "turn": turn,
        "command": "down",
        "normalized_action": "move",
        "valid": True,
        "status": {
            "current_room": room,
            "visited_levels": ["level_A", *(["level_B"] if room == "level_B" else [])],
            "board_state_hash": state_hash,
            "gem_count": gems,
            "moved": moved,
            "player": {"x": turn, "y": 1, "elevation": 0},
        },
    }


class MetricsTest(unittest.TestCase):
    def test_novelty_rooms_and_plateaus(self):
        metrics = derive_metrics(
            [
                action(1, "level_A", "one"),
                action(2, "level_A", "two"),
                action(3, "level_A", "two", moved=False),
                action(4, "level_B", "three", gems=1),
            ],
            {"token_usage": {"input_tokens": 96, "output_tokens": 4}},
        )
        summary = metrics["summary"]
        self.assertEqual(summary["unique_states"], 3)
        self.assertEqual(summary["revisited_states"], 1)
        self.assertEqual(summary["rooms_visited"], 2)
        self.assertEqual(summary["gems"], 1)
        self.assertEqual(summary["longest_plateau"], 1)
        self.assertEqual(summary["total_tokens"], 100)
        self.assertEqual(metrics["timeline"][-1]["gem_delta"], 1)
        self.assertTrue(metrics["timeline"][0]["moved"])
        self.assertEqual([room["room"] for room in metrics["rooms"]], ["level_A", "level_B"])

    def test_behavior_metrics_capture_repetition_failure_and_oscillation(self):
        rows = [
            action(1, "level_A", "one", moved=True),
            action(2, "level_A", "one", moved=False),
            action(3, "level_A", "two", moved=True),
            action(4, "level_A", "two", moved=False),
            action(5, "level_B", "three", gems=1, moved=True),
        ]
        for row, command in zip(rows, ["left", "right", "left", "right", "right"]):
            row["command"] = command

        metrics = derive_metrics(rows)
        summary = metrics["summary"]
        self.assertEqual(summary["failed_actions"], 2)
        self.assertEqual(summary["longest_failed_action_streak"], 1)
        self.assertEqual(summary["longest_command_streak"], 2)
        self.assertEqual(summary["oscillations"], 1)
        self.assertEqual(summary["first_gem_action"], 5)
        self.assertEqual(summary["first_room_transition_action"], 5)
        self.assertAlmostEqual(summary["action_entropy_bits"], 0.971, places=3)
        self.assertAlmostEqual(summary["failed_action_rate"], 0.4)
        self.assertTrue(metrics["timeline"][3]["oscillation"])
        self.assertEqual(metrics["timeline"][4]["command_streak"], 2)

    def test_live_jsonl_is_visible_before_final_actions_file(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-live"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(json.dumps({"status": "running"}))
            record = action(1, "level_A", "one")
            (run_dir / "actions.jsonl").write_text(json.dumps(record) + "\n{partial")
            run = RunStore(Path(directory)).load("run-live")
            self.assertIsNotNone(run)
            self.assertEqual(run["status"], "running")
            self.assertEqual(run["metrics"]["summary"]["actions"], 1)
            self.assertTrue(run["artifacts"]["actions"])

    def test_live_interactions_supply_provider_performance_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-live"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(json.dumps({"status": "running"}))
            (run_dir / "actions.jsonl").write_text(
                json.dumps(action(1, "level_A", "one")) + "\n"
            )
            records = [
                {
                    "call": 1,
                    "latency_ms": 1200,
                    "response": {
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "prompt_tokens_details": {"cached_tokens": 80},
                        },
                        "timings": {
                            "prompt_n": 20,
                            "prompt_ms": 250,
                            "predicted_n": 20,
                            "predicted_ms": 2000,
                        },
                    },
                },
                {
                    "call": 2,
                    "inherited_from": "run-parent",
                    "response": {
                        "usage": {"prompt_tokens": 999, "completion_tokens": 999}
                    },
                },
            ]
            (run_dir / "interactions.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n"
            )

            run = RunStore(Path(directory)).load("run-live")
            performance = run["metrics"]["performance"]
            self.assertEqual(performance["calls"], 1)
            self.assertEqual(performance["total_tokens"], 120)
            self.assertEqual(performance["cached_input_tokens"], 80)
            self.assertEqual(performance["prompt_tokens_per_second"], 80)
            self.assertEqual(performance["output_tokens_per_second"], 10)
            self.assertEqual(run["metrics"]["summary"]["total_tokens"], 120)
            self.assertEqual(run["metrics"]["summary"]["tokens_per_action"], 120)

    def test_path_breaks_identify_deaths_resets_and_room_entries(self):
        rows = [
            action(1, "level_A", "one"),
            action(2, "level_A", "two"),
            action(3, "level_A", "three"),
            action(4, "level_A", "four"),
            action(5, "level_B", "five"),
        ]
        rows[2]["status"]["player_dead"] = True
        rows[3]["command"] = "reset"
        rows[3]["normalized_action"] = "reset"

        timeline = derive_metrics(rows)["timeline"]

        self.assertEqual(
            [row["path_break_reason"] for row in timeline],
            ["room-entry", None, "death", "reset", "room-entry"],
        )


if __name__ == "__main__":
    unittest.main()
