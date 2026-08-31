import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mblab.control import (
    CONTEXT_MODES,
    MODEL_PROFILES,
    WEB_ROOT,
    ControlState,
    content_type_for,
    web_asset_for,
)


class ControlStateTest(unittest.TestCase):
    def test_web_assets_are_selected_from_an_explicit_allowlist(self):
        self.assertEqual(web_asset_for("/"), (WEB_ROOT / "index.html").resolve())
        self.assertEqual(web_asset_for("/app.js"), (WEB_ROOT / "app.js").resolve())
        for request_path in (
            "/unknown.js",
            "/../pyproject.toml",
            "/%2e%2e/pyproject.toml",
            "/app.js%0d%0aX-Injected:%20yes",
        ):
            with self.subTest(request_path=request_path):
                self.assertIsNone(web_asset_for(request_path))

    def test_hidden_application_views_override_layout_display_rules(self):
        styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertIn("[hidden] { display: none !important; }", styles)

    def test_content_types_come_only_from_a_fixed_registry(self):
        self.assertEqual(
            content_type_for(Path("filename\r\nX-Injected: yes.html")),
            "text/html; charset=utf-8",
        )
        self.assertEqual(
            content_type_for(Path("unknown\r\nX-Injected: yes.extension")),
            "application/octet-stream",
        )

    def test_control_center_exposes_managed_model_and_context_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            capabilities = state.capabilities()
            self.assertEqual(
                capabilities["model_profiles"][0]["id"], "local-openai"
            )
            self.assertEqual(
                capabilities["context_modes"][0]["id"], "generic-autocompact"
            )
            self.assertEqual(
                capabilities["context_modes"][1]["id"], "none"
            )
            self.assertEqual(capabilities["observation_modes"][0]["id"], "ascii")
            self.assertEqual(capabilities["observation_modes"][1]["id"], "json")
            self.assertEqual(capabilities["representation_contract_version"], 2)
            self.assertEqual(capabilities["benchmark_contract_status"], "passed")
            self.assertIn(
                "You are controlling a 3D grid game",
                capabilities["system_prompts"]["ascii_hidden"],
            )
            self.assertIn(
                "stable random identity for this run",
                capabilities["system_prompts"]["ascii_hidden"],
            )
            self.assertIn("local-openai", MODEL_PROFILES)
            self.assertIn("generic-autocompact", CONTEXT_MODES)
            self.assertIn("none", CONTEXT_MODES)

    def test_trial_uses_selected_managed_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                job = state.start_trial(
                    {
                        "model_profile": "local-openai",
                        "context_mode": "generic-autocompact",
                        "actions": 12,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["model"], "local")
            self.assertEqual(
                worker_config["base_url"], "http://127.0.0.1:8080/v1"
            )
            self.assertEqual(
                worker_config["context_mode"], "generic-autocompact"
            )
            self.assertEqual(worker_config["ascii_character_mode"], "canonical")
            self.assertFalse(worker_config["hide_names"])
            self.assertEqual(worker_config["hide_names_seed"], "")
            self.assertEqual(job["status"], "queued")

    def test_repetitions_are_queued_as_one_sequential_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                job = state.start_trial({"actions": 12, "repetitions": 3, "sampling_seed": 7})
            call = thread_class.call_args
            self.assertEqual(call.kwargs["target"], state._trial_batch_worker)
            job_ids, configs = call.kwargs["args"]
            self.assertEqual(len(job_ids), 3)
            self.assertEqual([item["sampling_seed"] for item in configs], [7, 8, 9])
            self.assertEqual(len({item["experiment_id"] for item in configs}), 1)
            self.assertEqual(len(job["run_ids"]), 3)

    def test_stopping_one_batch_job_cancels_queued_siblings(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread"):
                first = state.start_trial({"actions": 12, "repetitions": 3})
            state.jobs[first["id"]]["status"] = "running"
            state.stop_job(first["id"])
            statuses = {
                job["status"]
                for job in state.jobs.values()
                if job.get("experiment_id") == first["experiment_id"]
            }
            self.assertEqual(statuses, {"stopping", "cancelled"})

    def test_trial_ignores_submitted_prompt_without_unofficial_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                state.start_trial(
                    {
                        "actions": 12,
                        "system_prompt": "silently replace the benchmark prompt",
                        "unofficial_system_prompt": False,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["system_prompt"], "official")
            self.assertTrue(worker_config["system_prompt_matches_official"])
            self.assertNotEqual(
                worker_config["system_prompt_text"],
                "silently replace the benchmark prompt",
            )

    def test_trial_accepts_explicit_unofficial_system_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            prompt = "Experimental prompt\nKeep exact whitespace."
            with patch("mblab.control.threading.Thread") as thread_class:
                state.start_trial(
                    {
                        "actions": 12,
                        "unofficial_system_prompt": True,
                        "system_prompt": prompt,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["system_prompt"], "unofficial")
            self.assertEqual(worker_config["system_prompt_text"], prompt)
            self.assertFalse(worker_config["system_prompt_matches_official"])
            self.assertEqual(
                worker_config["system_prompt_sha256"],
                hashlib.sha256(prompt.encode()).hexdigest(),
            )

    def test_trial_rejects_empty_unofficial_system_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                state.start_trial(
                    {
                        "actions": 12,
                        "unofficial_system_prompt": True,
                        "system_prompt": "   ",
                    }
                )

    def test_trial_accepts_endpoint_managed_context_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                state.start_trial(
                    {
                        "model_profile": "local-openai",
                        "context_mode": "none",
                        "actions": 12,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["context_mode"], "none")

    def test_trial_accepts_official_json_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                state.start_trial(
                    {
                        "model_profile": "local-openai",
                        "observation_mode": "json",
                        "ascii_character_mode": "random",
                        "hide_names": True,
                        "hide_names_seed": "must-be-ignored",
                        "actions": 12,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["observation_mode"], "json")
            self.assertEqual(worker_config["system_prompt"], "official")
            self.assertIsNone(worker_config["ascii_character_mode"])
            self.assertFalse(worker_config["hide_names"])
            self.assertEqual(worker_config["hide_names_seed"], "")
            self.assertIn(
                "json_observation.objects",
                worker_config["system_prompt_text"],
            )
            self.assertIn(
                "Object type names are literal",
                worker_config["system_prompt_text"],
            )

    def test_trial_accepts_random_ascii_characters_with_selected_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                state.start_trial(
                    {
                        "observation_mode": "ascii",
                        "ascii_character_mode": "random",
                        "hide_names_seed": "representation-study-7",
                        "actions": 12,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["ascii_character_mode"], "random")
            self.assertTrue(worker_config["hide_names"])
            self.assertEqual(
                worker_config["hide_names_seed"], "representation-study-7"
            )
            self.assertIn(
                "stable random identity",
                worker_config["system_prompt_text"],
            )

    def test_trial_can_branch_from_a_verified_parent_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "runs" / "run-parent"
            parent.mkdir(parents=True)
            (parent / "run.json").write_text(
                json.dumps(
                    {
                        "schema_version": 5,
                        "status": "completed",
                        "config": {
                            "level": "level_HxI",
                            "hide_names": True,
                            "profile": "official-ascii-hidden-native-contract",
                        },
                    }
                )
            )
            (parent / "actions.jsonl").write_text(
                json.dumps(
                    {
                        "turn": 1,
                        "valid": True,
                        "command": "up",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "status": {"board_state_hash": "checkpoint"},
                    }
                )
                + "\n"
            )
            prompt = [
                {"role": "system", "content": "old"},
                {"role": "user", "content": "observation"},
            ]
            (parent / "interactions.jsonl").write_text(
                json.dumps(
                    {
                        "call": 1,
                        "request": {
                            "message_count": 2,
                            "shared_prefix_messages": 0,
                            "appended_messages": prompt,
                        },
                        "response": {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "up",
                                    }
                                }
                            ]
                        },
                    }
                )
                + "\n"
            )
            state = ControlState(root, root / "runs")
            with patch("mblab.control.threading.Thread") as thread_class:
                state.start_trial(
                    {
                        "fork_parent_run_id": "run-parent",
                        "fork_turn": 1,
                        "actions": 20,
                        "hide_names": False,
                    }
                )
            worker_config = thread_class.call_args.kwargs["args"][1]
            self.assertEqual(worker_config["fork"]["mode"], "active-context")
            self.assertEqual(worker_config["fork"]["parent_run_id"], "run-parent")
            self.assertEqual(worker_config["fork"]["turn"], 1)
            self.assertTrue(worker_config["hide_names"])
            self.assertEqual(worker_config["level"], "level_HxI")

    def test_delete_moves_completed_run_to_recoverable_trash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_dir = runs / "run-finished"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps({"status": "completed", "id": "run-finished"})
            )
            state = ControlState(root, runs)
            result = state.delete_run("run-finished")
            self.assertFalse(run_dir.exists())
            self.assertTrue(Path(result["recoverable_from"]).is_dir())

    def test_delete_refuses_running_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            run_dir = runs / "run-live"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({"status": "running"}))
            state = ControlState(root, runs)
            with self.assertRaisesRegex(ValueError, "running experiment"):
                state.delete_run("run-live")

    def test_delete_experiment_moves_all_repetitions_to_trash(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory), Path(directory) / "runs")
            for index in (1, 2):
                run_dir = state.store.root / f"run-repeat-{index}"
                run_dir.mkdir(parents=True)
                (run_dir / "run.json").write_text(
                    json.dumps({
                        "id": run_dir.name,
                        "status": "completed",
                        "config": {"experiment_id": "experiment-example"},
                    })
                )
            result = state.delete_experiment("experiment-example")
            self.assertEqual(set(result["deleted_runs"]), {"run-repeat-1", "run-repeat-2"})
            self.assertFalse((state.store.root / "run-repeat-1").exists())
            self.assertEqual(len(list((state.store.root / ".trash").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
