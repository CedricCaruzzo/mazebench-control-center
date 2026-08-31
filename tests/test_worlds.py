import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mblab.worlds import MazeBenchWorldService, namespace_runtime_text


class MazeBenchWorldServiceTest(unittest.TestCase):
    def make_world(self, root: Path, game_id: str, levels: dict[str, list[str]], *, title: str | None = None) -> None:
        world = root / "games" / game_id
        (world / "levels").mkdir(parents=True)
        (world / "world_map.json").write_text(json.dumps({"levels": levels}))
        (world / "world_parsing.json").write_text(json.dumps({"rules": {"world_size": [2, 1]}}))
        for file_name in levels:
            (world / "levels" / file_name).write_text("p .\n. G\n")
        if title:
            (world / "draft.json").write_text(json.dumps({"title": title, "default_level_id": "level_AxA"}))

    def service(self, directory: str) -> MazeBenchWorldService:
        root = Path(directory)
        self.make_world(root / "official", "maze", {"first.txt": ["A", "A"], "second.txt": ["B", "A"]})
        self.make_world(root / "workspace", "draft-controlled", {"level_AxA.txt": ["A", "A"]}, title="Controlled room")
        script = root / "level-state.js"
        script.write_text("// mocked")
        return MazeBenchWorldService(
            official_root=root / "official",
            repo_root=root,
            runs_root=root / "runs",
            level_state_script=script,
            workspace_root=root / "workspace",
        )

    def test_catalog_lists_read_only_official_world_and_editable_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.service(directory).catalog()
        self.assertEqual([world["id"] for world in catalog["worlds"]], ["maze", "draft-controlled"])
        self.assertFalse(catalog["worlds"][0]["editable"])
        self.assertTrue(catalog["worlds"][1]["editable"])

    def test_room_detail_uses_native_engine_state(self):
        state = {
            "levelId": "level_AxA", "width": 2, "height": 2,
            "actors": [{"type": "player"}, {"type": "gem"}],
            "terrain": [[{"type": "floor", "layers": []}, {"type": "ice", "layers": []}]],
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(state), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            with patch("mblab.worlds.subprocess.run", return_value=completed):
                detail = service.room_detail("maze", "level_AxA")
        self.assertEqual(detail["actor_counts"], {"gem": 1, "player": 1})
        self.assertIn({"name": "ice", "count": 1}, detail["mechanics"])
        self.assertEqual(detail["neighbors"], [{"direction": "right", "room": "level_BxA"}])

    def test_proxy_namespacing_and_official_mutation_guard(self):
        transformed = namespace_runtime_text('href="/"; href="/build"; href="/agent"; value.split("/")')
        self.assertIn('href="/maze/build"', transformed)
        self.assertIn('href="/maze/agent"', transformed)
        self.assertIn('split("/")', transformed)
        self.assertTrue(MazeBenchWorldService.protected_mutation("POST", "/api/author/maze/level_AxA"))
        self.assertFalse(MazeBenchWorldService.protected_mutation("POST", "/api/author/draft-controlled/level_AxA"))


if __name__ == "__main__":
    unittest.main()
