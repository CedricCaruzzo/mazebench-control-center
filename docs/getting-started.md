# Getting started

This guide takes a new checkout through installation, model configuration, and
the first trial.

## 1. Install the application

Requirements:

- macOS or Linux;
- Python 3.11–3.13 and `uv`;
- Node.js 20 or newer, npm, and FFmpeg for generated 3D replay and room authoring;
- an OpenAI-compatible chat-completions endpoint, or a local model command the
  Control Center can start.

Clone and install the locked environment:

```bash
git clone https://github.com/CedricCaruzzo/mazebench-control-center.git
cd mazebench-control-center
uv venv --python 3.12
uv sync --locked
```

Check the official MazeBench package, local tools, and configured model
profiles:

```bash
.venv/bin/mazebench-control-center doctor
```

The model may be reported as `offline` when using a managed profile: its
process starts only when a trial selects it. Missing MazeBench or Verifiers
packages must be fixed before running an experiment. Node/npm and FFmpeg are
needed only for replay generation.

Install the pinned replay dependencies once:

```bash
.venv/bin/mazebench-control-center setup-replay
```

## 2. Configure a model

Copy the safe example and edit the copy:

```bash
cp configs/models.example.toml configs/models.local.toml
```

Use the `local_llamacpp` profile when the Control Center should launch
`llama-server`. Set its model path in the shell instead of writing a private
path into Git-tracked configuration:

```bash
export MAZEBENCH_MODEL_PATH='/path/to/model.gguf'
```

Use `external_openai_compatible` when the endpoint is already running. If it
requires a key, export the environment variable named by `api_key_env`:

```bash
export MAZEBENCH_API_KEY='your-key'
```

See [Model profiles](model-profiles.md) for all fields and examples.

## 3. Validate the selected configuration

Pass the same profile file to diagnostics and the server:

```bash
.venv/bin/mazebench-control-center doctor \
  --config configs/models.local.toml
```

The report distinguishes externally operated endpoints from managed launch
commands. A reachable endpoint also reports its advertised model identifiers.

## 4. Start the Control Center

```bash
.venv/bin/mazebench-control-center control-center \
  --config configs/models.local.toml
```

Leave that terminal running and open <http://127.0.0.1:8787>. The status card
in the lower-left corner should say that the official MazeBench contract is
audited.

For a remote machine, keep the server bound to loopback and create an SSH
tunnel from the computer running the browser:

```bash
ssh -NT -o ExitOnForwardFailure=yes \
  -L 18787:127.0.0.1:8787 your-remote-host
```

Open <http://127.0.0.1:18787> locally. A successful `ssh -NT` command stays
silent and occupied until you stop the tunnel; that is expected.

## 5. Launch the first trial

1. Select **New trial**.
2. Choose the configured model profile.
3. Choose **JSON · structured coordinates** for the most directly readable
   baseline, or ASCII for the perspective-text condition.
4. Choose a context-management condition.
5. Keep **Clean environment · no parent** for an independent trial.
6. Set a small action budget for the first smoke test, such as 8–16 actions.
7. Leave the audited system prompt read-only.
8. Select **Launch trial**.

The selected managed model starts first. The run then appears in the archive
as queued/running and updates as action records are completed. Read
[Configure a new experiment](new-experiment.md) before comparing research
conditions or enabling prompt interventions.

## 6. Find the resulting data

Runs are stored under `runs/<run-id>/` by default. This directory is ignored by
Git. The web interface derives its charts from the saved action journal, so a
run remains inspectable without a database.

Before sharing data, use the sanitized exporter described in
[Inspect, replay, and fork runs](inspecting-runs.md#export-a-run-safely).
