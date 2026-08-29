# Troubleshooting

Start with the read-only diagnostic report:

```bash
.venv/bin/mazebench-control-center doctor \
  --config configs/models.local.toml
```

Add `--json` when attaching a sanitized report to an issue.

## The browser cannot reach the Control Center

On the server host:

```bash
curl http://127.0.0.1:8787/api/health
```

If it cannot connect, the Control Center process is not listening. Start it in
its own terminal and keep that terminal open:

```bash
.venv/bin/mazebench-control-center control-center \
  --config configs/models.local.toml
```

If the server is remote, `127.0.0.1:8787` in your laptop browser refers to the
laptop—not the remote host—unless a port is forwarded. Run this on the laptop:

```bash
ssh -NT -o ExitOnForwardFailure=yes \
  -L 18787:127.0.0.1:8787 your-remote-host
```

Then open <http://127.0.0.1:18787>. The SSH terminal remaining blank after
authentication means the `-N` tunnel is active. Stop it with Ctrl-C when done.

On Windows PowerShell or Command Prompt, the equivalent is:

```powershell
ssh.exe -NT -o ExitOnForwardFailure=yes -L 18787:127.0.0.1:8787 your-remote-host
```

Use a different local port if 18787 is occupied. Keep the Control Center bound
to `127.0.0.1`; it has no application authentication and should not be exposed
directly to an untrusted network.

## The model profile is missing from New trial

The menu is configuration-driven. Restart the backend with the intended file:

```bash
.venv/bin/mazebench-control-center control-center \
  --config configs/models.local.toml
```

Refreshing only the browser cannot change backend profiles. A supplied file
replaces the built-in profile rather than merging with it.

## The model endpoint is unavailable

Check the exact API root and model name:

```bash
curl http://127.0.0.1:8080/v1/models
```

For a managed profile, inspect **Run logs** or the run's `model-server.log`.
Common causes are:

- `llama-server` is not on `PATH`;
- `MAZEBENCH_MODEL_PATH` is missing or points to the wrong GGUF;
- `api_model` does not match the server alias;
- the profile and server use different ports;
- a remote key environment variable was not exported before startup.

Do not paste an API key or an unsanitized model-service log into a public issue.

## A request exceeds the context window

An error such as `request (...) exceeds the available context size` means the
outgoing conversation is larger than the model server accepts.

Check both sides:

- `context_window` documents the profile/harness capacity;
- the model process must be launched with a matching context size;
- output/reasoning tokens also need headroom;
- generic compaction must trigger below the actual endpoint limit;
- endpoint-managed mode requires the endpoint or wrapper to compact, or the
  full history will keep growing.

Lowering the action budget avoids the immediate failure but does not test
long-horizon context behavior. Treat any compaction threshold or wrapper as a
recorded experimental condition.

## A local request times out during a long prompt

Prompt ingestion can take many minutes near a large context window, especially
on a local model. A client read timeout may cancel a still-processing model
request. Inspect the run error chain and model log to distinguish:

- slow prompt evaluation;
- context overflow;
- a stopped/restarted model process;
- a malformed assistant response;
- an unreachable endpoint.

Compacting earlier reduces repeated prompt evaluation. Increasing only the
HTTP timeout prevents cancellation but does not reduce the computational cost.

## Thinking returns no final action

Some reasoning models can consume the entire completion budget before emitting
ordinary `content` or a tool/action result. Increase the thinking budget only
enough to produce a stable final action, or disable thinking for the baseline.
Larger reasoning budgets sharply increase latency and context growth.

The Control Center saves returned reasoning separately. **Preserve previous
thinking verbatim** controls future model input; it does not control whether
the raw trace is journaled.

## 3D replay is unavailable or fails

Install dependencies once:

```bash
.venv/bin/mazebench-control-center setup-replay
```

Run `doctor` and confirm Node.js, npm, and FFmpeg are available. Then regenerate
the replay from the completed run. If it fails, inspect
`runs/<run-id>/artifacts/replay.log`.

The synchronized interactive viewer and generated MP4 are separate: the
viewer reconstructs an individual decision on demand, while video generation
renders the complete trajectory.

## The UI looks stale after an update

The browser and Python backend must come from the same checkout. Finish or stop
active work, restart the Control Center process, and hard-refresh the browser.
The New Trial form disables launching when it detects an older backend missing
the current experiment contract.

## A run was deleted accidentally

Deletion is recoverable. Stop the Control Center, locate the timestamped
directory under `runs/.trash/`, and move it back under `runs/` with a valid,
unique run ID. Never modify a running run directory.

## Asking for help

Use [GitHub Discussions](https://github.com/CedricCaruzzo/mazebench-control-center/discussions)
for setup questions and a structured issue for reproducible bugs. Follow
[SUPPORT.md](../SUPPORT.md): include the version and sanitized diagnostics, but
never publish credentials, private prompts, raw reasoning, or unsanitized run
directories.
