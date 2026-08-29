import tempfile
import unittest
from pathlib import Path

from mblab.config import default_model_profiles, load_model_profiles, public_model_profile


class ModelConfigTest(unittest.TestCase):
    def test_default_profile_is_portable_and_unmanaged(self):
        root = Path(__file__).resolve().parents[1]
        profile = default_model_profiles(root)["local-openai"]

        self.assertEqual(profile["provider"], "openai-compatible")
        self.assertEqual(profile["context_window"], 65_536)
        self.assertEqual(profile["launch_command"], [])

    def test_external_profile_requires_no_local_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "models.toml"
            config.write_text(
                """
[models.remote]
provider = "openai-compatible"
api_model = "remote-model"
base_url = "https://inference.example/v1"
context_window = 131072
"""
            )

            profiles = load_model_profiles(Path(directory), config)

            self.assertEqual(list(profiles), ["remote"])
            self.assertEqual(profiles["remote"]["launch_command"], [])
            self.assertFalse(public_model_profile(profiles["remote"])["managed"])

    def test_public_profile_hides_command_and_environment(self):
        profile = {
            "id": "private",
            "label": "Private",
            "api_model": "model",
            "base_url": "http://127.0.0.1:8000/v1",
            "launch_command": ["secret-launcher"],
            "launch_cwd": "/private/path",
            "environment": {"API_KEY": "secret"},
        }

        public = public_model_profile(profile)

        self.assertTrue(public["managed"])
        self.assertNotIn("launch_command", public)
        self.assertNotIn("launch_cwd", public)
        self.assertNotIn("environment", public)


if __name__ == "__main__":
    unittest.main()
