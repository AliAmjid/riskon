"""``riskon`` command line: nine commands, zero problem-specific logic."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import paths
from .db import connect, md_table, open_run
from .solvers import CHEATSHEET, doctor


def _add_run_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--run",
        default=None,
        help="run directory to operate on (defaults to the current run)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riskon",
        description="Operations Research workstation: data in, verified optimal decision out.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check every solver backend and the workbench")
    sub.add_parser("solvers", help="print the solver selection cheatsheet")

    p_new = sub.add_parser("new", help="scaffold a run directory with an empty workbench")
    p_new.add_argument("slug", help="short name for the run, e.g. fleet-procurement")
    p_new.add_argument(
        "--template",
        default=None,
        help="template to copy in as model.py (see templates/); omit to skip",
    )

    p_load = sub.add_parser("load", help="convert a file or URL into a canonical table")
    p_load.add_argument("source", help="path or http(s) URL: csv, tsv, parquet, json, xlsx")
    p_load.add_argument("--table", default="source", help="destination table name")
    p_load.add_argument("--sheet", default=0, help="worksheet name or index for Excel inputs")
    p_load.add_argument("--no-profile", action="store_true", help="skip the profile output")
    _add_run_flag(p_load)

    p_sql = sub.add_parser("sql", help="run SQL against the run's workbench")
    p_sql.add_argument("query", help="SQL to execute")
    p_sql.add_argument("--limit", type=int, default=50, help="max rows to display")
    _add_run_flag(p_sql)

    p_profile = sub.add_parser("profile", help="re-print the profile for a table")
    p_profile.add_argument("table", nargs="?", default="source")
    _add_run_flag(p_profile)

    p_export = sub.add_parser("export", help="dump a run's tables to csv or parquet")
    p_export.add_argument("run", nargs="?", default=None, help="run directory")
    p_export.add_argument("--format", default="csv", choices=["csv", "parquet"])
    p_export.add_argument("--into", default=None, help="destination directory")

    p_publish = sub.add_parser(
        "publish",
        help="copy a run's deliverables into artifacts/ so they leave the machine",
    )
    p_publish.add_argument("run", nargs="?", default=None, help="run directory")
    p_publish.add_argument(
        "--into",
        default=None,
        help="destination directory (defaults to artifacts/ at the repo root)",
    )

    sub.add_parser("runs", help="list run directories")

    return parser


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_doctor() -> int:
    report, healthy = doctor()
    print(report)
    return 0 if healthy else 1


def cmd_solvers() -> int:
    print(CHEATSHEET)
    return 0


def cmd_new(slug: str, template: str | None) -> int:
    run_dir = paths.new_run_dir(slug)
    paths.set_current_run(run_dir)

    with connect(run_dir) as wb:
        wb.set_meta(slug=slug, run_dir=str(run_dir))

    if template:
        name = template if template.endswith(".py") else f"{template}.py"
        source = paths.templates_dir() / name
        if not source.exists():
            available = sorted(p.stem for p in paths.templates_dir().glob("*.py"))
            print(f"no template {name!r}. Available: {', '.join(available)}", file=sys.stderr)
            return 1
        shutil.copy(source, run_dir / "model.py")

    print(f"run     {run_dir}")
    print(f"artifact {run_dir / 'workbench.duckdb'}")
    if template:
        print(f"model   {run_dir / 'model.py'} (from {template})")
    print("\nThis run is now current; riskon load/sql will use it.")
    return 0


def _require_run(run: str | None) -> Path:
    resolved = Path(run) if run else paths.current_run()
    if resolved is None:
        print(
            "no active run. Start one first:\n  riskon new <slug>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return resolved


def cmd_load(source: str, table: str, sheet: str, run: str | None, no_profile: bool) -> int:
    run_dir = _require_run(run)
    try:
        sheet_arg: str | int = int(sheet)
    except (TypeError, ValueError):
        sheet_arg = sheet

    with connect(run_dir) as wb:
        wb.load(source, table=table, sheet=sheet_arg)
        print(f"loaded {source} -> table {table!r} ({wb.row_count(table):,} rows)")
        if not no_profile:
            print()
            print(wb.profile(table))
    return 0


def cmd_sql(query: str, limit: int, run: str | None) -> int:
    run_dir = _require_run(run)
    with connect(run_dir) as wb:
        frame = wb.sql(query)
    if frame.empty:
        print("(no rows)")
        return 0
    print(md_table(frame, limit=limit))
    if len(frame) > limit:
        print(f"\n... {len(frame):,} rows total, showing first {limit}")
    return 0


def cmd_profile(table: str, run: str | None) -> int:
    run_dir = _require_run(run)
    with connect(run_dir) as wb:
        if not wb.has_table(table):
            known = ", ".join(wb.tables()) or "none"
            print(f"no table {table!r}. Tables: {known}", file=sys.stderr)
            return 1
        print(wb.profile(table))
    return 0


def cmd_export(run: str | None, fmt: str, into: str | None) -> int:
    run_dir = _require_run(run)
    destination = Path(into) if into else Path(run_dir) / "export"
    with open_run(run_dir) as wb:
        written = wb.export(destination, fmt=fmt)
    for path in written:
        print(path)
    return 0


# The three files a run always produces, and the tables worth handing over as
# spreadsheets. `source` and `candidates` are deliberately absent: they are
# inputs, they can be huge, and workbench.duckdb already carries them.
_PUBLISH_FILES = ("report.md", "model.py", "workbench.duckdb")
_PUBLISH_TABLES = {"solution": "decision.csv", "constraints": "constraints.csv"}


def cmd_publish(run: str | None, into: str | None) -> int:
    run_dir = _require_run(run)
    destination = Path(into) if into else paths.artifacts_dir()
    destination.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    missing: list[str] = []

    for name in _PUBLISH_FILES:
        source = Path(run_dir) / name
        if not source.exists():
            missing.append(name)
            continue
        target = destination / name
        shutil.copy2(source, target)
        written.append(target)

    workbench = Path(run_dir) / "workbench.duckdb"
    if workbench.exists():
        with open_run(run_dir) as wb:
            for table, filename in _PUBLISH_TABLES.items():
                if not wb.has_table(table):
                    missing.append(f"{table} table")
                    continue
                frame = wb.sql(f"SELECT * FROM {table}")
                if frame.empty:
                    missing.append(f"{table} table (empty)")
                    continue
                target = destination / filename
                frame.to_csv(target, index=False)
                written.append(target)

    for path in written:
        print(f"{path.stat().st_size:>10,} B  {path}")

    if missing:
        print(f"\nnot published (absent): {', '.join(missing)}", file=sys.stderr)

    if not written:
        print(
            f"\nnothing to publish from {run_dir}. Solve the model and write "
            "report.md first; artifacts/ is the only path off this machine.",
            file=sys.stderr,
        )
        return 1

    print(f"\n{len(written)} file(s) published to {destination}")
    return 0


def cmd_runs() -> int:
    root = paths.runs_dir()
    if not root.exists():
        print("(no runs yet)")
        return 0
    current = paths.current_run()
    found = False
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        marker = "*" if current and path.resolve() == current.resolve() else " "
        print(f"{marker} {path.name}")
        found = True
    if not found:
        print("(no runs yet)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "solvers":
        return cmd_solvers()
    if args.command == "new":
        return cmd_new(args.slug, args.template)
    if args.command == "load":
        return cmd_load(args.source, args.table, args.sheet, args.run, args.no_profile)
    if args.command == "sql":
        return cmd_sql(args.query, args.limit, args.run)
    if args.command == "profile":
        return cmd_profile(args.table, args.run)
    if args.command == "export":
        return cmd_export(args.run, args.format, args.into)
    if args.command == "publish":
        return cmd_publish(args.run, args.into)
    if args.command == "runs":
        return cmd_runs()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
