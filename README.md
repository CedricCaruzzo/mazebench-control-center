# MazeBench Control Center

[![Tests](https://github.com/CedricCaruzzo/mazebench-control-center/actions/workflows/tests.yml/badge.svg)](https://github.com/CedricCaruzzo/mazebench-control-center/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/CedricCaruzzo/mazebench-control-center)](https://github.com/CedricCaruzzo/mazebench-control-center/releases)
[![License](https://img.shields.io/github/license/CedricCaruzzo/mazebench-control-center)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11--3.13-3776AB.svg)](https://www.python.org/)

A local web interface for running, inspecting, replaying, and branching
[MazeBench](https://mazebench.com/) experiments. It uses the installed official
MazeBench engine and keeps each run as a filesystem journal that can be
inspected without a database.

This is an independent community tool, not an official MazeBench project.
Version `0.x` is an early public research release: run artifacts are designed
for auditability, but command-line and artifact schemas may still evolve.

## What it provides

- trial launch and live progress from a browser;
- exact action, observation, response, reasoning, and compaction journals;
- 3D replay plus synchronized model-observation views;
- run statistics, room coverage, movement, and novelty charts;
- recontextualized forks from verified turn checkpoints;
- selectable ASCII/JSON observations and optional generic context compaction;
- managed or externally operated OpenAI-compatible model endpoints;
- sanitized run export for sharing.

## Requirements

- macOS or Linux (Windows users can run the server on a remote host and use an
  SSH tunnel);
- Python 3.11–3.13 and `uv`;
- Node.js/npm for 3D replay generation;
- an OpenAI-compatible chat-completions endpoint such as llama.cpp or vLLM.

Model weights and experiment runs are deliberately not included.

## Install

```bash
git clone https://github.com/CedricCaruzzo/mazebench-control-center.git
cd mazebench-control-center
uv venv --python 3.12
uv sync --locked
.venv/bin/mazebench-control-center doctor
```

For exact 3D replay support:

```bash
.venv/bin/mazebench-control-center setup-replay
```

## Configure a model

The no-config default expects an already-running model named `local` at
`http://127.0.0.1:8080/v1`. For any real experiment, create a local profile:

```bash
cp configs/models.example.toml configs/models.local.toml
```

Edit `configs/models.local.toml` for your endpoint. Profiles can either point
to an external service or define a `launch_command`; a managed command starts
when a trial selects that profile and is stopped when no longer needed. Local
profile files and credentials are ignored by Git.

The example includes:

- a managed `llama-server` command using `MAZEBENCH_MODEL_PATH`;
- an externally managed OpenAI-compatible endpoint.

Qwen reasoning support is enabled per profile with
`thinking_contract = "qwen"`. Use `"none"` for models without that contract.

The model list is intentionally configuration-driven: it contains the built-in
`local-openai` profile when no configuration file is supplied, or exactly the
profiles in the selected TOML file. The Control Center probes a selected
endpoint when checking or starting it, but does not scan the machine or network
and add detected services automatically.

API keys are read from the environment-variable name in `api_key_env`; their
values are never sent to or stored by the browser UI. For example:

```bash
export MAZEBENCH_API_KEY='your-key'
```

## Context management

Each trial chooses one of two explicit conditions:

- **Generic automatic compaction** asks the selected model for a neutral
  continuation summary and retains a recent verbatim tail. Every compaction is
  journaled.
- **Endpoint-managed / no Control Center compaction** forwards the normal
  growing conversation unchanged. Choose this when an upstream agent harness
  or compatible endpoint deliberately manages context itself, or when testing
  full history within the endpoint's context window.

An OpenAI-compatible chat-completions server is normally stateless. Merely
placing Claude Code, Codex, or another agent behind that wire format does not
guarantee context management: the wrapper must explicitly compact or otherwise
manage the incoming message history.

## Run

```bash
.venv/bin/mazebench-control-center control-center \
  --config configs/models.local.toml
```

Open <http://127.0.0.1:8787>. Run data is written under `runs/`, which is
ignored by Git. To use the UI across SSH without exposing it to the network:

```bash
ssh -NT -L 18787:127.0.0.1:8787 your-remote-host
```

Then open <http://127.0.0.1:18787> locally.

## Share a run safely

Raw runs may contain reasoning, prompts, logs, provider errors, and absolute
paths. Create a redacted archive and inspect it before sharing:

```bash
.venv/bin/mazebench-control-center export-run runs/run-id
```

Reasoning, logs, replay media, and unofficial prompts are excluded by default.
Explicit flags opt them back in.

## Benchmark boundary

The repository pins MazeBench and verifies benchmark-critical package,
prompt, glyph, level, view, and scoring assumptions before a trial. Context
management, representation changes, unofficial prompts, and forks are harness
interventions and are recorded in each run manifest. They should be treated as
distinct experimental conditions rather than official leaderboard parity.

## Development

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
node --check web/app.js
node --check web/viewer.js
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Release history is recorded in
[CHANGELOG.md](CHANGELOG.md), and research citation metadata is available in
[CITATION.cff](CITATION.cff). The project is licensed under the Apache License
2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
