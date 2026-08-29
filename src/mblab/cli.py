"""Command-line entry point for MazeBench Control Center."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "control-center":
        from mblab.control import main as control_main

        return control_main(arguments[1:])
    if arguments and arguments[0] == "run":
        from mblab.smoke import main as run_main

        return run_main(arguments[1:])

    parser = argparse.ArgumentParser(prog="mazebench-control-center")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("control-center", help="start the local web app")
    subparsers.add_parser("run", help="run one experiment without the web app")

    doctor = subparsers.add_parser("doctor", help="check local runtime capabilities")
    doctor.add_argument("--config", type=Path, default=None)
    doctor.add_argument("--json", action="store_true")

    subparsers.add_parser(
        "setup-replay",
        help="install pinned browser/render dependencies for official replay",
    )

    export = subparsers.add_parser("export-run", help="create a sanitized public ZIP")
    export.add_argument("run", type=Path)
    export.add_argument("--output", type=Path, default=None)
    export.add_argument("--include-reasoning", action="store_true")
    export.add_argument("--include-logs", action="store_true")
    export.add_argument("--include-replay", action="store_true")
    export.add_argument("--include-unofficial-prompt", action="store_true")

    args = parser.parse_args(arguments)
    if args.command == "doctor":
        from mblab.doctor import diagnostics, print_diagnostics

        repo_root = Path.cwd().resolve()
        report = diagnostics(repo_root, args.config)
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        return 0 if print_diagnostics(report) else 1
    if args.command == "setup-replay":
        from mblab.replay_setup import setup_replay

        repo_root = Path.cwd().resolve()
        print(json.dumps(setup_replay(repo_root), indent=2))
        return 0
    if args.command == "export-run":
        from mblab.export import export_run

        output = args.output or Path(f"{args.run.name}.public.zip")
        report = export_run(
            args.run,
            output,
            include_reasoning=args.include_reasoning,
            include_logs=args.include_logs,
            include_replay=args.include_replay,
            include_unofficial_prompt=args.include_unofficial_prompt,
        )
        print(json.dumps(report, indent=2))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
