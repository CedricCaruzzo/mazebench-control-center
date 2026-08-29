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
