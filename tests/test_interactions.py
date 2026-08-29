import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mblab.interactions import InteractionJournal, install_openai_interaction_journal
from mblab.store import RunStore, read_jsonl


class InteractionJournalTest(unittest.TestCase):
    def test_fork_journal_continues_parent_call_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = InteractionJournal(
                Path(directory) / "interactions.jsonl", call_offset=12
            )
            pending = journal.begin(
                prompt=[{"role": "user", "content": "continued observation"}],
                model="local",
                sampling_args={},
                tools=None,
            )
            self.assertEqual(pending["call"], 13)

    def test_installed_wrapper_captures_native_response(self):
        from verifiers.clients.openai_chat_completions_client import (
            OpenAIChatCompletionsClient,
        )

        with tempfile.TemporaryDirectory() as directory:
            original = OpenAIChatCompletionsClient.get_native_response

            async def fake_native_response(self, prompt, model, sampling_args, tools=None, **kwargs):
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "left",
                                "reasoning_content": "The left tile is open.",
                            },
                        }
                    ]
                }

            try:
                OpenAIChatCompletionsClient.get_native_response = fake_native_response
                path = Path(directory) / "interactions.jsonl"
                install_openai_interaction_journal(path)
                instance = object.__new__(OpenAIChatCompletionsClient)
                response = asyncio.run(
                    OpenAIChatCompletionsClient.get_native_response(
                        instance,
                        [{"role": "user", "content": "Choose."}],
                        "local",
                        {"max_tokens": 128},
                    )
                )
                self.assertEqual(response["choices"][0]["message"]["content"], "left")
                records = read_jsonl(path)
                self.assertEqual(
                    records[0]["response"]["choices"][0]["message"]["reasoning_content"],
                    "The left tile is open.",
                )
            finally:
                OpenAIChatCompletionsClient.get_native_response = original

    def test_retains_reasoning_and_lossless_prompt_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs" / "run-live" / "interactions.jsonl"
            journal = InteractionJournal(path)
            first_prompt = [
                {"role": "system", "content": "Choose one action."},
                {"role": "user", "content": "Start."},
            ]
            pending = journal.begin(
                prompt=first_prompt,
                model="local",
                sampling_args={"max_tokens": 640},
                tools=None,
            )
            journal.finish(
                pending,
                response={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "reasoning_content": "The path above is clear.",
                                "content": "up",
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8},
                },
            )

            second_prompt = [
                *first_prompt,
                {
                    "role": "assistant",
                    "reasoning_content": "The path above is clear.",
                    "content": "up",
                },
                {"role": "user", "content": "The player moved."},
            ]
            pending = journal.begin(
                prompt=second_prompt,
                model="local",
                sampling_args={"max_tokens": 640},
                tools=None,
            )
            journal.finish(pending, error=RuntimeError("server unavailable"))

            records = read_jsonl(path)
            self.assertEqual(len(records), 2)
            message = records[0]["response"]["choices"][0]["message"]
            self.assertEqual(message["reasoning_content"], "The path above is clear.")
            self.assertEqual(message["content"], "up")
            self.assertEqual(records[1]["request"]["shared_prefix_messages"], 2)
            self.assertEqual(records[1]["request"]["appended_messages"], second_prompt[2:])
            self.assertEqual(records[1]["error"]["type"], "RuntimeError")

    def test_store_reads_live_journal_and_ignores_partial_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run_dir = runs / "run-live"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(json.dumps({"status": "running"}))
            record = {"schema_version": 1, "call": 1, "response": {"choices": []}}
            (run_dir / "interactions.jsonl").write_text(json.dumps(record) + "\n{partial")

            store = RunStore(runs)
            self.assertEqual(store.load_interactions("run-live"), [record])
            self.assertTrue(store.load("run-live")["artifacts"]["interactions"])

            compaction = {"compaction": 1, "status": "started", "before_call": 2}
            (run_dir / "compactions.jsonl").write_text(
                json.dumps(compaction) + "\n{partial"
            )
            self.assertEqual(store.load_compactions("run-live"), [compaction])

    def test_store_pairs_exact_model_ascii_with_action_and_replay_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run_dir = runs / "run-observation"
            artifacts = run_dir / "artifacts"
            artifacts.mkdir(parents=True)
            (run_dir / "run.json").write_text(json.dumps({"status": "completed"}))
            (run_dir / "actions.json").write_text(
                json.dumps(
                    [
                        {
                            "turn": 1,
                            "command": "up",
                            "status": {
                                "current_room": "level_HxI",
                                "level": "POST ACTION",
                            },
                        }
                    ]
                )
            )
            (run_dir / "interactions.jsonl").write_text(
                json.dumps(
                    {
                        "call": 1,
                        "request": {
                            "appended_messages": [
                                {
                                    "role": "user",
                                    "content": "Observation:\n```text\nEXACT INPUT\n```\nChoose.",
                                }
                            ]
                        },
                    }
                )
                + "\n"
            )
            manifest = {"fps": 24, "first_action_frame": 12, "actions": []}
            (artifacts / ".maze_replay_manifest.json").write_text(json.dumps(manifest))

            run = RunStore(runs).load("run-observation")
            self.assertEqual(run["metrics"]["timeline"][0]["model_board"], "EXACT INPUT")
            self.assertEqual(run["replay_manifest"], manifest)

    def test_store_pairs_exact_model_json_with_action(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run_dir = runs / "run-json-observation"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "config": {"observation_mode": "json"},
                    }
                )
            )
            (run_dir / "actions.json").write_text(
                json.dumps([{"turn": 1, "command": "up", "status": {}}])
            )
            exact = '{\n  "schema_version": 2,\n  "objects": {"P": [[4, 12, 0]]}\n}'
            (run_dir / "interactions.jsonl").write_text(
                json.dumps(
                    {
                        "call": 1,
                        "request": {
                            "appended_messages": [
                                {
                                    "role": "user",
                                    "content": f"Observation:\n```json\n{exact}\n```",
                                }
                            ]
                        },
                    }
                )
                + "\n"
            )

            run = RunStore(runs).load("run-json-observation")
            self.assertEqual(run["metrics"]["timeline"][0]["model_board"], exact)


if __name__ == "__main__":
    unittest.main()
