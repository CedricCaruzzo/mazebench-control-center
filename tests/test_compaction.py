import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mblab.compaction import (
    GenericAutoCompactor,
    estimated_chat_tokens,
    summary_timeout_seconds,
)
from mblab.store import read_jsonl


class GenericAutoCompactorTest(unittest.TestCase):
    def test_compacts_old_history_and_keeps_recent_verbatim_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            summaries = []

            async def count_tokens(messages, model, sampling_args, tools):
                return len(json.dumps(messages)) // 4

            async def summarize(source, model, budget, timeout_seconds):
                summaries.append(source)
                self.assertGreaterEqual(timeout_seconds, 900)
                return "The earlier task state and unresolved work were retained.", {
                    "usage": {"prompt_tokens": 400, "completion_tokens": 12}
                }

            compactor = GenericAutoCompactor(
                base_url="http://127.0.0.1:8080/v1",
                journal_path=Path(directory) / "compactions.jsonl",
                compact_at_tokens=700,
                source_compact_at_tokens=500,
                summary_budget_tokens=256,
                recent_turns=2,
                token_counter=count_tokens,
                summary_generator=summarize,
            )
            raw = [{"role": "system", "content": "Official instructions."}]
            for turn in range(1, 7):
                raw.extend(
                    [
                        {
                            "role": "user",
                            "content": f"Observation {turn}: " + "A" * 180,
                        },
                        {
                            "role": "assistant",
                            "reasoning_content": f"Reasoning {turn}: " + "B" * 80,
                            "content": "left",
                        },
                    ]
                )
            raw.append({"role": "user", "content": "Current observation."})

            bounded = asyncio.run(
                compactor.prepare(raw, "qwen", {"max_tokens": 16}, None)
            )

            self.assertEqual(bounded[0], raw[0])
            self.assertIn("conversation-summary", bounded[1]["content"])
            self.assertEqual(bounded[2:], raw[-4:])
            self.assertEqual(compactor.compaction_count, 1)
            self.assertTrue(summaries)
            # Reasoning is model-owned data and remains available to the
            # neutral compactor, while evaluator-private state never enters.
            self.assertIn("reasoning_content", json.dumps(summaries[0]))

            events = read_jsonl(Path(directory) / "compactions.jsonl")
            self.assertEqual(
                [event["status"] for event in events],
                ["started", "completed"],
            )
            completed = events[-1]
            self.assertEqual(completed["mode"], "generic-autocompact")
            self.assertEqual(completed["before_call"], 7)
            self.assertEqual(completed["recent_turns_retained"], 2)
            self.assertLess(completed["working_tokens_after"], 700)

    def test_reuses_checkpoint_and_appends_new_raw_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            async def count_tokens(messages, model, sampling_args, tools):
                return len(json.dumps(messages))

            async def summarize(source, model, budget, timeout_seconds):
                return "Stable checkpoint.", {"usage": {}}

            compactor = GenericAutoCompactor(
                base_url="http://127.0.0.1:8080/v1",
                journal_path=Path(directory) / "compactions.jsonl",
                compact_at_tokens=700,
                source_compact_at_tokens=600,
                summary_budget_tokens=256,
                recent_turns=1,
                token_counter=count_tokens,
                summary_generator=summarize,
            )
            raw = [{"role": "system", "content": "System."}]
            for turn in range(5):
                raw.extend(
                    [
                        {"role": "user", "content": "X" * 120},
                        {"role": "assistant", "content": f"move-{turn}"},
                    ]
                )
            raw.append({"role": "user", "content": "Current."})
            first = asyncio.run(
                compactor.prepare(raw, "qwen", {"max_tokens": 16}, None)
            )
            raw.extend(
                [
                    {"role": "assistant", "content": "right"},
                    {"role": "user", "content": "Next."},
                ]
            )
            second = asyncio.run(
                compactor.prepare(raw, "qwen", {"max_tokens": 16}, None)
            )

            self.assertEqual(first[1]["content"], second[1]["content"])
            self.assertEqual(second[-2:], raw[-2:])

    def test_large_fork_history_can_compact_more_than_four_times(self):
        with tempfile.TemporaryDirectory() as directory:
            async def count_tokens(messages, model, sampling_args, tools):
                return len(json.dumps(messages))

            async def summarize(source, model, budget, timeout_seconds):
                return "Small rolling checkpoint.", {"usage": {}}

            compactor = GenericAutoCompactor(
                base_url="http://127.0.0.1:8080/v1",
                journal_path=Path(directory) / "compactions.jsonl",
                compact_at_tokens=700,
                source_compact_at_tokens=600,
                summary_budget_tokens=256,
                recent_turns=1,
                token_counter=count_tokens,
                summary_generator=summarize,
            )
            raw = [{"role": "system", "content": "System."}]
            for turn in range(12):
                raw.extend(
                    [
                        {"role": "user", "content": "X" * 180},
                        {"role": "assistant", "content": f"move-{turn}"},
                    ]
                )
            raw.append({"role": "user", "content": "Current."})

            bounded = asyncio.run(
                compactor.prepare(raw, "qwen", {"max_tokens": 16}, None)
            )

            self.assertGreater(compactor.compaction_count, 4)
            self.assertLess(len(json.dumps(bounded)), 700)

    def test_failed_summary_is_durably_journaled(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "compactions.jsonl"

            async def count_tokens(messages, model, sampling_args, tools):
                return len(json.dumps(messages))

            async def summarize(source, model, budget, timeout_seconds):
                raise TimeoutError("summary deadline reached")

            compactor = GenericAutoCompactor(
                base_url="http://127.0.0.1:8080/v1",
                journal_path=journal,
                compact_at_tokens=700,
                source_compact_at_tokens=600,
                summary_budget_tokens=256,
                recent_turns=1,
                token_counter=count_tokens,
                summary_generator=summarize,
            )
            raw = [{"role": "system", "content": "System."}]
            for turn in range(5):
                raw.extend(
                    [
                        {"role": "user", "content": "X" * 120},
                        {"role": "assistant", "content": f"move-{turn}"},
                    ]
                )
            raw.append({"role": "user", "content": "Current."})

            with self.assertRaises(TimeoutError):
                asyncio.run(compactor.prepare(raw, "qwen", {"max_tokens": 16}, None))

            events = read_jsonl(journal)
            self.assertEqual(
                [event["status"] for event in events],
                ["started", "failed"],
            )
            self.assertEqual(events[-1]["error"]["type"], "TimeoutError")
            self.assertEqual(compactor.compaction_count, 0)

    def test_fork_call_offset_uses_global_call_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            async def count_tokens(messages, model, sampling_args, tools):
                return len(json.dumps(messages))

            async def summarize(source, model, budget, timeout_seconds):
                return "Checkpoint.", {"usage": {}}

            compactor = GenericAutoCompactor(
                base_url="http://127.0.0.1:8080/v1",
                journal_path=Path(directory) / "compactions.jsonl",
                compact_at_tokens=700,
                source_compact_at_tokens=600,
                recent_turns=1,
                call_offset=120,
                inherited_assistant_count=5,
                token_counter=count_tokens,
                summary_generator=summarize,
            )
            raw = [{"role": "system", "content": "System."}]
            for turn in range(5):
                raw.extend(
                    [
                        {"role": "user", "content": "X" * 120},
                        {"role": "assistant", "content": f"move-{turn}"},
                    ]
                )
            raw.append({"role": "user", "content": "Current."})

            asyncio.run(compactor.prepare(raw, "qwen", {"max_tokens": 16}, None))

            completed = read_jsonl(Path(directory) / "compactions.jsonl")[-1]
            self.assertEqual(completed["before_call"], 121)

    def test_summary_timeout_scales_with_source_and_output_budget(self):
        self.assertEqual(summary_timeout_seconds(1_000, 256), 900)
        self.assertEqual(summary_timeout_seconds(32_000, 2_048), 1_332)

    def test_provider_neutral_token_estimate_counts_unicode_bytes(self):
        ascii_count = estimated_chat_tokens([{"role": "user", "content": "A" * 90}])
        unicode_count = estimated_chat_tokens([{"role": "user", "content": "◆" * 90}])

        self.assertGreater(unicode_count, ascii_count)


if __name__ == "__main__":
    unittest.main()
