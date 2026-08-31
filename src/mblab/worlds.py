"""Discover MazeBench worlds and bridge its native local room builder."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any


GAME_ID = re.compile(r"^(?:maze|(?:draft|online)-[a-z0-9-]{4,40})$")
LEVEL_ID = re.compile(r"^level_([A-Z])x([A-Z])$")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Root-relative URLs emitted by MazeBench's Node site must stay below the
# Control Center's /maze reverse-proxy namespace.
RUNTIME_ROOTS = (
    "api/", "assets/", "author/", "world-map/", "build", "agent", "agent/",
    "train", "play/", "flyover/",
    "games/", "vendor/", "utils/", "logos/", "styles.css", "site.css",
    "local-site.css", "build-theme.css", "author-theme.css", "play-theme.css",
    "build.js", "agent.js", "agent-run.js", "train.js", "author.js",
    "author-shell.js", "author-play-data.js",
    "author-solver-worker.js", "play.js", "play-rules.js", "play-core.js",
    "play-render-effects.js", "play-render-terrain.js", "play-render-actors.js",
    "play-render-three.js", "play-render-compositor.js", "play-render.js",
    "play-movement.js", "play-world-transitions.js", "play-gameplay.js",
    "flyover.js", "maze-engine.js", "maze-solver.js", "world-solver.js",
    "world-solver-worker.js", "maze-token-patterns.js", "level-preview.js",
    "world-map.js", "favicon.svg",
)


def namespace_runtime_text(value: str) -> str:
    # Keep the native site's logo/home links inside the embedded authoring
    # surface rather than recursively loading the Control Center in its iframe.
    value = value.replace('href="/"', 'href="/maze/build"')
    value = value.replace("href='/'", "href='/maze/build'")
    for route in RUNTIME_ROOTS:
        for marker in ('"', "'", "`", "(", "="):
            value = value.replace(f"{marker}/{route}", f"{marker}/maze/{route}")
    return value


def namespace_runtime_value(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("/") and any(value[1:].startswith(route) for route in RUNTIME_ROOTS):
            return f"/maze{value}"
        return value
    if isinstance(value, list):
        return [namespace_runtime_value(item) for item in value]
    if isinstance(value, dict):
        return {key: namespace_runtime_value(item) for key, item in value.items()}
    return value


def protect_official_build_card(value: str) -> str:
    value = value.replace(
        "The master benchmark world. Edits here change the world agents are scored on.",
        "Pinned reference world. The Control Center keeps it read-only; duplicate it to experiment.",
    )
    return re.sub(
        r'<a class="button" href="/maze/author/maze/[^\"]+">Edit</a>',
        '<span class="button" aria-disabled="true">Read only</span>',
        value,
        count=1,
    )


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _letters_index(value: str) -> int:
    result = 0
    for character in value.upper():
        result = result * 26 + ord(character) - 64
    return result - 1


class MazeBenchWorldService:
    """Inspect the official world and local drafts through the native engine."""

    def __init__(
        self,
        *,
        official_root: Path,
        repo_root: Path,
        runs_root: Path,
        level_state_script: Path,
        workspace_root: Path | None = None,
    ):
        self.official_root = official_root.resolve()
        self.repo_root = repo_root.resolve()
        self.runs_root = runs_root.resolve()
        self.level_state_script = level_state_script.resolve()
        self._workspace_root = workspace_root.resolve() if workspace_root else None
        self._server_lock = threading.RLock()
        self._server_process: subprocess.Popen[Any] | None = None
        self._server_port: int | None = None
        self._room_cache: dict[tuple[str, str, int, int], dict[str, Any]] = {}

    @property
    def workspace_root(self) -> Path:
        if self._workspace_root is None:
            from mazebench_cli import resolve_root

            self._workspace_root = Path(resolve_root()).resolve()
        return self._workspace_root

    def _world_root(self, game_id: str) -> Path:
        if game_id == "maze":
            return self.official_root
        if not GAME_ID.fullmatch(game_id):
            raise FileNotFoundError(game_id)
        return self.workspace_root

    def _world_dir(self, game_id: str) -> Path:
        root = self._world_root(game_id)
        games = (root / "games").resolve()
        world = (games / game_id).resolve()
        if games not in world.parents or not world.is_dir():
            raise FileNotFoundError(game_id)
        return world

    def _world_descriptor(self, game_id: str) -> dict[str, Any]:
        world_dir = self._world_dir(game_id)
        mappings = (_read_json(world_dir / "world_map.json", {}).get("levels") or {})
        if not isinstance(mappings, dict):
            raise FileNotFoundError(game_id)
        rooms = []
        for file_name, coordinates in mappings.items():
            if not (isinstance(file_name, str) and isinstance(coordinates, list) and len(coordinates) == 2):
                continue
            column, row = map(str, coordinates)
            level_id = f"level_{column}x{row}"
            if LEVEL_ID.fullmatch(level_id):
                rooms.append({
                    "id": level_id,
                    "label": f"{column}x{row}",
                    "column": _letters_index(column),
                    "row": _letters_index(row),
                    "file_name": file_name,
                })
        rooms.sort(key=lambda room: (room["row"], room["column"]))
        if not rooms:
            raise FileNotFoundError(game_id)
        parsing = _read_json(world_dir / "world_parsing.json", {})
        size = ((parsing.get("rules") or {}).get("world_size") or [])
        width = int(size[0]) if len(size) == 2 else max(room["column"] for room in rooms) + 1
        height = int(size[1]) if len(size) == 2 else max(room["row"] for room in rooms) + 1
        meta = _read_json(world_dir / "draft.json", {}) if game_id != "maze" else {}
        room_ids = {room["id"] for room in rooms}
        default_level = str(meta.get("default_level_id") or "")
        if default_level not in room_ids:
            default_level = "level_HxI" if "level_HxI" in room_ids else rooms[0]["id"]
        return {
            "id": game_id,
            "title": "Maze Bench Environment" if game_id == "maze" else str(meta.get("title") or game_id),
            "kind": "official" if game_id == "maze" else "draft",
            "editable": game_id != "maze",
            "width": width,
            "height": height,
            "room_count": len(rooms),
            "default_level_id": default_level,
            "updated_at": meta.get("updated_at"),
            "rooms": rooms,
        }

    def catalog(self) -> dict[str, Any]:
        worlds = [self._world_descriptor("maze")]
        games_dir = self.workspace_root / "games"
        if games_dir.is_dir():
            for path in sorted(games_dir.iterdir()):
                if path.name == "maze" or not GAME_ID.fullmatch(path.name):
                    continue
                try:
                    worlds.append(self._world_descriptor(path.name))
                except FileNotFoundError:
                    continue
        return {
            "worlds": worlds,
            "workspace_kind": "mazebench-local",
            "official_world_read_only": True,
        }

    def _room_source(self, game_id: str, level_id: str) -> tuple[Path, str]:
        descriptor = self._world_descriptor(game_id)
        room = next((item for item in descriptor["rooms"] if item["id"] == level_id), None)
        if not room:
            raise FileNotFoundError(level_id)
        path = self._world_dir(game_id) / "levels" / room["file_name"]
        if not path.is_file():
            raise FileNotFoundError(level_id)
        return path, path.read_text(encoding="utf-8")

    def room_detail(self, game_id: str, level_id: str) -> dict[str, Any]:
        if not GAME_ID.fullmatch(game_id) or not LEVEL_ID.fullmatch(level_id):
            raise FileNotFoundError(level_id)
        source_path, raw_text = self._room_source(game_id, level_id)
        stat = source_path.stat()
        cache_key = (game_id, level_id, stat.st_mtime_ns, stat.st_size)
        if cache_key in self._room_cache:
            return self._room_cache[cache_key]
        root = self._world_root(game_id)
        completed = subprocess.run(
            [shutil.which("node") or "node", str(self.level_state_script), str(root), level_id, game_id],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "MazeBench level parser failed")
        level_state = namespace_runtime_value(json.loads(completed.stdout))
        actor_counts = Counter(
            str(actor.get("type") or "unknown")
            for actor in level_state.get("actors") or []
            if isinstance(actor, dict)
        )
        terrain_counts: Counter[str] = Counter()
        for row in level_state.get("terrain") or []:
            for cell in row or []:
                if not isinstance(cell, dict):
                    continue
                terrain_counts[str(cell.get("type") or "unknown")] += 1
                for layer in cell.get("layers") or []:
                    if isinstance(layer, dict) and layer.get("type"):
                        terrain_counts[str(layer["type"])] += 1
        ignored = {"empty", "floor", "wall", "player", "unknown"}
        mechanics = [
            {"name": name.replace("_", " "), "count": count}
            for name, count in sorted((actor_counts + terrain_counts).items())
            if name not in ignored
        ]
        world = self._world_descriptor(game_id)
        coordinates = next(room for room in world["rooms"] if room["id"] == level_id)
        room_ids = {room["id"] for room in world["rooms"]}
        neighbors = []
        for label, dx, dy in (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1)):
            x, y = coordinates["column"] + dx, coordinates["row"] + dy
            if 0 <= x < 26 and 0 <= y < 26:
                candidate = f"level_{chr(65 + x)}x{chr(65 + y)}"
                if candidate in room_ids:
                    neighbors.append({"direction": label, "room": candidate})
        result = {
            "game_id": game_id,
            "world_title": world["title"],
            "world_kind": world["kind"],
            "editable": world["editable"],
            "level_id": level_id,
            "label": level_id.removeprefix("level_"),
            "width": int(level_state.get("width") or 0),
            "height": int(level_state.get("height") or 0),
            "raw_text": raw_text,
            "source_file": coordinates["file_name"],
            "source_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
            "actor_counts": dict(sorted(actor_counts.items())),
            "terrain_counts": dict(sorted(terrain_counts.items())),
            "mechanics": mechanics,
            "neighbors": neighbors,
            "level_state": level_state,
        }
        self._room_cache = {key: value for key, value in self._room_cache.items() if key[:2] != (game_id, level_id)}
        self._room_cache[cache_key] = result
        return result

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def ensure_server(self) -> int:
        with self._server_lock:
            if self._server_process is not None and self._server_process.poll() is None and self._server_port is not None:
                return self._server_port
            root = self.workspace_root
            port = self._free_port()
            self.runs_root.mkdir(parents=True, exist_ok=True)
            log_path = self.runs_root / ".builder-server.log"
            environment = os.environ.copy()
            environment.update(HOST="127.0.0.1", PORT=str(port), MAZEBENCH_STATE_FILE=str(self.runs_root / ".builder-server-state.json"))
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [shutil.which("node") or "node", str(root / "server.js")],
                    cwd=root,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            deadline = time.monotonic() + 8
            last_error = ""
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request("GET", "/build", headers={"Host": f"127.0.0.1:{port}"})
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    if response.status == 200:
                        self._server_process, self._server_port = process, port
                        return port
                    last_error = f"HTTP {response.status}"
                except OSError as exc:
                    last_error = str(exc)
                time.sleep(0.1)
            if process.poll() is None:
                process.terminate()
            raise RuntimeError(f"MazeBench room builder did not start{': ' + last_error if last_error else '; see ' + str(log_path)}")

    @staticmethod
    def protected_mutation(method: str, target_path: str) -> bool:
        if method.upper() not in UNSAFE_METHODS:
            return False
        path = target_path.split("?", 1)[0]
        return bool(
            re.match(r"^/api/(?:author|world-map)/maze(?:/|$)", path)
            or re.match(r"^/api/build/worlds/maze(?:/|$)", path)
        )

    def proxy(self, method: str, target_path: str, *, body: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        if self.protected_mutation(method, target_path):
            payload = json.dumps({"error": "The pinned benchmark world is read-only. Duplicate it into a draft before editing it."}).encode()
            return 403, {"Content-Type": "application/json; charset=utf-8"}, payload
        port = self.ensure_server()
        forwarded = {"Host": f"127.0.0.1:{port}", "Connection": "close"}
        for name in ("Accept", "Content-Type", "Range"):
            if headers and headers.get(name):
                forwarded[name] = headers[name]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        connection.request(method, target_path, body=body, headers=forwarded)
        response = connection.getresponse()
        payload = response.read()
        response_headers = {name: value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        content_type = response_headers.get("Content-Type", "")
        if content_type.startswith("text/") or "javascript" in content_type or "json" in content_type:
            try:
                text = namespace_runtime_text(payload.decode("utf-8"))
                if target_path.split("?", 1)[0] == "/build":
                    text = protect_official_build_card(text)
                payload = text.encode("utf-8")
            except UnicodeDecodeError:
                pass
        location = response_headers.get("Location")
        if location and location.startswith("/") and not location.startswith("/maze/"):
            response_headers["Location"] = f"/maze{location}"
        return status, response_headers, payload

    def shutdown(self) -> None:
        with self._server_lock:
            process, self._server_process, self._server_port = self._server_process, None, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
