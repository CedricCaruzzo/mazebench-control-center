# Model profiles

Model choices are server-side TOML profiles. The browser never asks for or
stores an API key, model path, or launch command.

Copy the example before editing:

```bash
cp configs/models.example.toml configs/models.local.toml
```

Files matching `configs/*.local.toml` are ignored by Git. You may also keep the
configuration entirely outside the repository.

## How profiles appear

Start the server with the selected file:

```bash
.venv/bin/mazebench-control-center control-center \
  --config configs/models.local.toml
```

The **Model** menu contains exactly the profiles in that file. Without
`--config`, it contains one neutral `local-openai` profile pointing at
`http://127.0.0.1:8080/v1`. The Control Center probes selected endpoints but
does not scan the computer or network and automatically add discovered models.

As an alternative to `--config`, set `MAZEBENCH_CC_CONFIG` before starting the
server. An explicit `--config` takes precedence.

## Profile fields

| Field | Purpose |
| --- | --- |
| `label` | Human-readable name shown in **New trial**. |
| `provider` | Endpoint family used for diagnostics and default token counting, such as `llama.cpp` or `openai-compatible`. |
| `api_model` | Model identifier sent to `/v1/chat/completions`. It must match the endpoint's served model/alias. |
| `base_url` | OpenAI-compatible API root, normally ending in `/v1`. |
| `api_key_env` | Name of the environment variable holding the key. Never put the key itself here. |
| `token_count_mode` | `llama.cpp` for provider token details or `estimate` for portable estimation. |
| `thinking_contract` | `qwen` enables Qwen reasoning controls; `none` hides unsupported controls. |
| `context_window` | Declared model context capacity used by the harness configuration. This does not change the model server's own limit. |
| `launch_command` | Optional command. When present, the profile is managed and the process starts for a selected trial. |
| `launch_cwd` | Working directory for the managed command, resolved relative to the configuration file unless absolute. |
| `environment` | Optional environment table supplied to the managed child process. Values support environment-variable and `~` expansion. |

String values in `launch_command`, `launch_cwd`, and `environment` expand shell
environment variables, but the command is executed as an argument vector—not
through a shell.

## Managed llama.cpp example

```toml
[models.qwen_local]
label = "Qwen local · llama.cpp"
provider = "llama.cpp"
api_model = "qwen-local"
base_url = "http://127.0.0.1:8080/v1"
api_key_env = "MAZEBENCH_API_KEY"
token_count_mode = "llama.cpp"
thinking_contract = "qwen"
context_window = 65536
launch_command = [
  "llama-server",
  "-m", "${MAZEBENCH_MODEL_PATH}",
  "--alias", "qwen-local",
  "--host", "127.0.0.1",
  "--port", "8080",
  "-c", "65536",
  "--jinja"
]
launch_cwd = ".."
```

Before starting the Control Center:

```bash
export MAZEBENCH_MODEL_PATH='/path/to/model.gguf'
```

The Control Center starts the command when this profile is selected, waits for
its OpenAI-compatible endpoint, records service metadata/logs with the run, and
stops only the child process it launched.

## External endpoint example

```toml
[models.remote_vllm]
label = "Remote vLLM endpoint"
provider = "openai-compatible"
api_model = "served-model-name"
base_url = "https://model-host.example/v1"
api_key_env = "MAZEBENCH_REMOTE_KEY"
token_count_mode = "estimate"
thinking_contract = "none"
context_window = 131072
```

```bash
export MAZEBENCH_REMOTE_KEY='your-key'
```

No `launch_command` means the service is externally managed. The Control
Center will not start or stop it.

## Context-management wrappers

A service can expose Claude, Codex, or another harness through an
OpenAI-compatible endpoint, but the protocol alone does not provide agent
memory or automatic compaction. When the wrapper truly manages its incoming
history, choose **Endpoint-managed / no Control Center compaction** and document
the wrapper/version as part of the experimental condition.

## Diagnose a profile

```bash
.venv/bin/mazebench-control-center doctor \
  --config configs/models.local.toml
```

Use `--json` for machine-readable diagnostics. The command reports whether a
managed launcher is available and whether an endpoint answered `/v1/models`;
it does not print API-key values or managed environment contents.
