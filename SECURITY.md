# Security

MazeBench Control Center is a local experiment controller. It can start model processes,
read complete reasoning journals, stop jobs, and move run directories to its
recoverable trash. Treat access to the Control Center as access to the account
running it.

## Supported deployment boundary

The server binds to `127.0.0.1` and has no application authentication. Keep that
default. For remote use, connect through an authenticated SSH tunnel or place an
authenticated reverse proxy in front of it. Do not expose the Control Center or
the model endpoint directly to an untrusted network.

Model-profile files may contain local commands and environment values. Keep
machine-specific files under `configs/*.local.toml` or outside the repository;
those names are ignored by Git. Never place API keys directly in a committed
profile. Prefer an environment-variable name understood by the provider.

Run journals may contain model reasoning, unofficial prompts, absolute paths,
provider errors, and model-service logs. Use `mazebench-control-center export-run` before
sharing a run and review the resulting archive. Reasoning, logs, replay data,
and unofficial prompts are excluded or redacted unless explicitly requested.

## Reporting a vulnerability

Until a public security contact is selected, report vulnerabilities using a
private GitHub Security Advisory on the repository. Do not include secrets or
private run artifacts in a public issue.
