"""Command-line interface for HUTrackDB.

    python -m hutrackdb build          # full pipeline -> all outputs
    python -m hutrackdb qa             # QA against the All U.S. Hurricanes list
    python -m hutrackdb gates --export  # write the default gate set out
    python -m hutrackdb provenance     # print the calibration register
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("pyogrio").setLevel(logging.WARNING)
    logging.getLogger("fiona").setLevel(logging.WARNING)


def command_build(args) -> int:
    from .config import Config
    from .db.snowflake import write_snowflake_ddl
    from .db.writers import write_all
    from .pipeline import run

    config = Config.load(args.config)
    print(config.provenance_report())

    result = run(config)
    written = write_all(result, config)
    ddl = write_snowflake_ddl(config)

    print("\n" + "=" * 70)
    print("BUILD COMPLETE")
    print("=" * 70)
    print(result.summary())
    for label, path in written.items():
        print(f"  {label:12s} {path}")
    print(f"  {'snowflake':12s} {ddl}")
    if result.warnings:
        print(f"\n{len(result.warnings)} parse warning(s):")
        for warning in result.warnings[:10]:
            print(f"  - {warning}")
    return 0


def command_qa(args) -> int:
    from .config import Config
    from .qa.validate import run_qa

    config = Config.load(args.config)
    report = run_qa(config, reference_path=args.reference)
    print(report.render())
    output = config.output_dir() / "qa_report.md"
    output.write_text(report.render())
    print(f"\nwritten: {output}")
    return 0 if report.passed else 1


def command_gates(args) -> int:
    from .config import Config
    from .geo.coastline import CoastlineSource
    from .geo.gates import build_gate_set

    config = Config.load(args.config)
    coastline = CoastlineSource.from_config(config)
    gate_set = build_gate_set(config, coastline)
    print(f"gate set: {len(gate_set)} gates  origin={gate_set.origin}")
    if args.export:
        target = Path(args.export)
        gate_set.to_file(target)
        print(f"exported to {target}")
        print("Edit this file, or replace it with your own, then set "
              "gates.override_path in config/pipeline.yaml. See docs/GATES.md.")
    return 0


def command_provenance(args) -> int:
    from .config import Config

    config = Config.load(args.config)
    print(config.provenance_report())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hutrackdb",
        description="Curated Atlantic/Pacific hurricane track database from NOAA HURDAT2.",
    )
    parser.add_argument("--config", help="path to pipeline.yaml", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="run the full pipeline")
    build.set_defaults(func=command_build)

    qa = subparsers.add_parser("qa", help="validate against the All U.S. Hurricanes list")
    qa.add_argument("--reference", default=None, help="path to the reference HTML/CSV")
    qa.set_defaults(func=command_qa)

    gates = subparsers.add_parser("gates", help="inspect or export the gate set")
    gates.add_argument("--export", default=None, help="write the gate set to this path")
    gates.set_defaults(func=command_gates)

    provenance = subparsers.add_parser(
        "provenance", help="print the calibration provenance register"
    )
    provenance.set_defaults(func=command_provenance)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
