#!/usr/bin/env node

// Emit one parsed level using the official MazeBench server implementation.
const path = require("node:path");

const runtimeRoot = path.resolve(process.argv[2] || "");
const levelId = String(process.argv[3] || "");

if (!runtimeRoot || !/^level_[A-Z]+x[A-Z]+$/.test(levelId)) {
  process.stderr.write("usage: official-level-state.js <runtime-root> <level-id>\n");
  process.exit(2);
}

const app = require(path.join(runtimeRoot, "server", "app.js"));
const game = app.getGame("maze");
const level = game && app.getLevel(game, levelId);

if (!level) {
  process.stderr.write(`unknown MazeBench level: ${levelId}\n`);
  process.exit(3);
}

process.stdout.write(`${JSON.stringify(app.getLevelState(game, level))}\n`);
