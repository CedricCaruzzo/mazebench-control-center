import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from mblab.export import export_run


class PublicRunExportTest(unittest.TestCase):
    def test_default_export_redacts_local_paths_reasoning_and_custom_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-private"
            run.mkdir()
            (run / "run.json").write_text(
                json.dumps(
                    {
                        "id": "run-private",
                        "config": {
                            "system_prompt": "unofficial",
                            "runtime": "/Users/private-person/project/.venv/runtime",
                        },
                    }
                )
            )
            (run / "system-prompt.txt").write_text("private experimental idea")
            (run / "interactions.jsonl").write_text(
                json.dumps(
                    {
                        "request": {
                            "appended_messages": [
                                {"role": "system", "content": "private experimental idea"},
                                {"role": "user", "content": "visible observation"},
                            ]
                        },
                        "response": {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "reasoning_content": "private reasoning",
                                        "content": "up",
                                    }
                                }
                            ]
                        },
                    }
                )
                + "\n"
            )
            (run / "runner.log").write_text("/Users/private-person/secret")
            output = root / "public.zip"

            report = export_run(run, output)

            self.assertFalse(report["privacy"]["reasoning_included"])
            self.assertTrue(report["privacy"]["unofficial_prompt_redacted"])
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertNotIn("run-private/system-prompt.txt", names)
                self.assertNotIn("run-private/runner.log", names)
                interaction = archive.read("run-private/interactions.jsonl").decode()
                manifest = archive.read("run-private/run.json").decode()
            self.assertNotIn("private reasoning", interaction)
            self.assertNotIn("private experimental idea", interaction)
            self.assertIn("<redacted-unofficial-system-prompt>", interaction)
            self.assertNotIn("private-person", manifest)
            self.assertIn("<local-home>", manifest)

    def test_sensitive_material_requires_explicit_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-opt-in"
            run.mkdir()
            (run / "run.json").write_text(
                json.dumps({"id": "run-opt-in", "config": {"system_prompt": "unofficial"}})
            )
            (run / "system-prompt.txt").write_text("chosen custom prompt")
            (run / "interactions.json").write_text(
                json.dumps([{"reasoning_content": "chosen reasoning"}])
            )
            (run / "runner.log").write_text("chosen log")
            output = root / "full.zip"

            export_run(
                run,
                output,
                include_reasoning=True,
                include_logs=True,
                include_unofficial_prompt=True,
            )

            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.read("run-opt-in/system-prompt.txt").decode(),
                    "chosen custom prompt",
                )
                self.assertIn(
                    "chosen reasoning",
                    archive.read("run-opt-in/interactions.json").decode(),
                )
                self.assertEqual(
                    archive.read("run-opt-in/runner.log").decode(),
                    "chosen log",
                )


if __name__ == "__main__":
    unittest.main()
