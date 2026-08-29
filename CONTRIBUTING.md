# Contributing

MazeBench Control Center separates the pinned official benchmark from local harness
interventions. Changes must preserve that boundary and label any condition that
changes prompts, observations, tools, context management, or evaluator-visible
information.

Before implementing a substantial change, open a focused issue so its benchmark
impact and artifact-compatibility requirements can be agreed on. Small fixes may
go directly to a pull request.

## Development setup

```bash
uv venv --python 3.12
uv sync --locked
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
node --check web/app.js
node --check web/viewer.js
```

Run `mazebench-control-center doctor` before a real rollout. Copy
`configs/models.example.toml` to `configs/models.local.toml` for machine-local
model profiles. Do not commit model paths, credentials, raw runs, or reasoning
traces.

## Change expectations

- Add tests for run schemas, prompt transformations, forks, providers, and
  context-management modes.
- Keep raw journals append-only and derived indexes disposable.
- Do not send evaluator-private render state or decoded identities to a model.
- Record new experimental interventions prominently in `run.json`.
- Preserve old run readability or provide an explicit migration.
- Keep the default HTTP listener on loopback.

Large refactors should include a small sanitized fixture demonstrating backward
compatibility. Do not use an unpublished real run as a test fixture.

## Contribution license

Unless explicitly stated otherwise, contributions intentionally submitted for
inclusion in this project are provided under the Apache License 2.0, without
additional terms or conditions, as described by Section 5 of that license.
