"""The DuckDB workbench.

One rule governs this module: **every input becomes a DuckDB table at the door,
and nothing downstream ever reads a source file again.** Model scripts,
verification and the report all read tables, so the agent only has to know one
access pattern no matter what format arrived.

Each run owns a single ``workbench.duckdb`` holding five tables:

``source``       every ingested row, canonicalised and keyed by ``row_id``
``candidates``   the narrowed set the model was actually built on
``solution``     the solver's decision per ``row_id``
``constraints``  one row per business rule, tagged with the sentence it encodes
``meta``         key/value record of the question, query, solver and assumptions
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import pandas as pd

from .paths import cache_dir, current_run

WORKBENCH_NAME = "workbench.duckdb"

CSV_SUFFIXES = {".csv", ".txt"}
TSV_SUFFIXES = {".tsv"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
JSON_SUFFIXES = {".json", ".ndjson", ".jsonl"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}

_IDENT_RE = re.compile(r"[^0-9a-zA-Z]+")


# --------------------------------------------------------------------------
# Canonicalisation
# --------------------------------------------------------------------------


def snake_case(name: str) -> str:
    """``Miles per Gallon`` -> ``miles_per_gallon``."""
    # Split camelCase before flattening, so `unitPrice` keeps its word boundary.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    cleaned = _IDENT_RE.sub("_", spaced).strip("_").lower()
    if not cleaned:
        cleaned = "column"
    if cleaned[0].isdigit():
        cleaned = f"col_{cleaned}"
    return cleaned


def canonical_columns(names: Sequence[str]) -> list[str]:
    """snake_case every column, disambiguating any collisions."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for name in names:
        base = snake_case(name)
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 0
        out.append(base)
    return out


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_url(source: str) -> bool:
    return urllib.parse.urlparse(str(source)).scheme in {"http", "https"}


def fetch_to_cache(url: str) -> Path:
    """Download a remote file once so re-runs are offline-safe."""
    import requests

    name = Path(urllib.parse.urlparse(url).path).name or "download"
    target = cache_dir() / f"{abs(hash(url)) % (10**10)}-{name}"
    if target.exists():
        return target

    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        tmp = target.with_suffix(target.suffix + ".part")
        with tmp.open("wb") as handle:
            shutil.copyfileobj(response.raw, handle)
        tmp.replace(target)
    return target


# --------------------------------------------------------------------------
# Constraint logging
# --------------------------------------------------------------------------


@dataclass
class Constraint:
    """A business rule and the mathematics it became.

    Carrying ``business_rule`` next to ``expression`` is what makes the
    natural-language-to-model translation auditable without a spec file.
    """

    name: str
    business_rule: str
    expression: str
    sense: str
    bound: float
    achieved: float | None = None

    @property
    def slack(self) -> float | None:
        if self.achieved is None:
            return None
        if self.sense in {"<=", "<"}:
            return self.bound - self.achieved
        if self.sense in {">=", ">"}:
            return self.achieved - self.bound
        return abs(self.bound - self.achieved)

    @property
    def binding(self) -> bool | None:
        slack = self.slack
        if slack is None:
            return None
        tolerance = max(1e-6, abs(self.bound) * 1e-9)
        return bool(abs(slack) <= tolerance)

    @property
    def satisfied(self) -> bool | None:
        slack = self.slack
        if slack is None:
            return None
        tolerance = max(1e-6, abs(self.bound) * 1e-9)
        if self.sense in {"==", "="}:
            return bool(abs(self.bound - self.achieved) <= tolerance)
        return bool(slack >= -tolerance)


@dataclass
class ConstraintLog:
    """Collects constraints as the model is built, for the artifact and report."""

    items: list[Constraint] = field(default_factory=list)

    def add(
        self,
        name: str,
        business_rule: str,
        expression: str,
        sense: str,
        bound: float,
    ) -> Constraint:
        constraint = Constraint(name, business_rule, expression, sense, float(bound))
        self.items.append(constraint)
        return constraint

    def set_achieved(self, name: str, achieved: float) -> None:
        for item in self.items:
            if item.name == name:
                item.achieved = float(achieved)
                return
        raise KeyError(f"no constraint named {name!r}")

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "name": c.name,
                    "business_rule": c.business_rule,
                    "expression": c.expression,
                    "sense": c.sense,
                    "bound": c.bound,
                    "achieved": c.achieved,
                    "slack": c.slack,
                    "binding": c.binding,
                    "satisfied": c.satisfied,
                }
                for c in self.items
            ]
        )

    def violations(self) -> list[Constraint]:
        return [c for c in self.items if c.satisfied is False]


# --------------------------------------------------------------------------
# Workbench
# --------------------------------------------------------------------------


class Workbench:
    """A single run's DuckDB database."""

    def __init__(self, path: Path | str | None = None, read_only: bool = False):
        if path is None:
            self.path = Path(":memory:")
            self.con = duckdb.connect()
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.con = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self._ensure_meta()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Workbench":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_meta(self) -> None:
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key        VARCHAR PRIMARY KEY,
                value      VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )

    # -- querying ----------------------------------------------------------

    def sql(self, query: str) -> pd.DataFrame:
        return self.con.sql(query).df()

    def tables(self) -> list[str]:
        rows = self.con.execute("SHOW TABLES").fetchall()
        return [row[0] for row in rows]

    def has_table(self, name: str) -> bool:
        return name in self.tables()

    def columns(self, table: str) -> list[str]:
        rows = self.con.execute(f"DESCRIBE {_quote(table)}").fetchall()
        return [row[0] for row in rows]

    def row_count(self, table: str) -> int:
        return int(self.con.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0])

    # -- ingestion ---------------------------------------------------------

    def load(
        self,
        source: str | Path | pd.DataFrame,
        table: str = "source",
        sheet: str | int = 0,
    ) -> str:
        """Convert any supported input into a canonical table.

        Canonical means: snake_cased column names, DuckDB-resolved types (never
        an opaque object column), and a synthetic ``row_id`` primary key that
        lets ``solution`` join back to ``source`` no matter what arrived.
        """
        if isinstance(source, pd.DataFrame):
            relation = "__riskon_inbound"
            self.con.register(relation, source)
            reader = _quote(relation)
            origin = "<dataframe>"
            local: Path | None = None
        else:
            origin = str(source)
            local = fetch_to_cache(origin) if _is_url(origin) else Path(origin).expanduser()
            if not local.exists():
                raise FileNotFoundError(f"no such file: {local}")
            reader = self._reader_for(local, sheet=sheet)

        described = self.con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()
        raw_columns = [row[0] for row in described]
        canonical = canonical_columns(raw_columns)
        projection = ",\n    ".join(
            f"{_quote(raw)} AS {_quote(clean)}" for raw, clean in zip(raw_columns, canonical)
        )

        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE {_quote(table)} AS
            SELECT
                (row_number() OVER ()) - 1 AS row_id,
                {projection}
            FROM {reader}
            """
        )

        if isinstance(source, pd.DataFrame):
            self.con.unregister("__riskon_inbound")

        self.set_meta(
            **{
                f"{table}_uri": origin,
                f"{table}_file": str(local) if local else "",
                f"{table}_rows": self.row_count(table),
                f"{table}_ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        return table

    def _reader_for(self, path: Path, sheet: str | int = 0) -> str:
        """Return a DuckDB relation expression for a local file."""
        suffix = path.suffix.lower()
        literal = str(path).replace("'", "''")

        if suffix in PARQUET_SUFFIXES:
            return f"read_parquet('{literal}')"
        if suffix in JSON_SUFFIXES:
            return f"read_json_auto('{literal}')"
        if suffix in TSV_SUFFIXES:
            return f"read_csv_auto('{literal}', delim='\t', sample_size=-1)"
        if suffix in EXCEL_SUFFIXES:
            # Routed through pandas rather than the DuckDB excel extension so
            # ingestion never depends on downloading an extension at run time.
            frame = pd.read_excel(path, sheet_name=sheet)
            self.con.register("__riskon_excel", frame)
            return _quote("__riskon_excel")
        if suffix in CSV_SUFFIXES or suffix == "":
            return f"read_csv_auto('{literal}', sample_size=-1)"
        raise ValueError(
            f"unsupported input format {suffix!r}. Supported: csv, tsv, parquet, json, xlsx."
        )

    # -- profiling ---------------------------------------------------------

    def profile(self, table: str = "source", top_n: int = 5) -> str:
        """Markdown profile: SUMMARIZE plus top values for categorical columns."""
        summary = self.sql(f"SUMMARIZE {_quote(table)}")
        rows = self.row_count(table)

        lines = [
            f"## `{table}` - {rows:,} rows x {len(self.columns(table))} columns",
            "",
            summary.to_markdown(index=False),
        ]

        categorical = summary.loc[
            summary["column_type"].astype(str).str.upper().str.startswith("VARCHAR"),
            "column_name",
        ].tolist()

        if categorical:
            lines += ["", f"### Categorical columns (top {top_n})", ""]
            for column in categorical:
                counts = self.sql(
                    f"""
                    SELECT {_quote(column)} AS value, count(*) AS n
                    FROM {_quote(table)}
                    GROUP BY 1
                    ORDER BY n DESC
                    LIMIT {int(top_n)}
                    """
                )
                rendered = ", ".join(f"{v!s} ({n:,})" for v, n in counts.itertuples(index=False))
                lines.append(f"- **{column}**: {rendered}")

        return "\n".join(lines)

    # -- the modelling seam ------------------------------------------------

    def materialize(self, query: str, table: str = "candidates") -> pd.DataFrame:
        """Freeze the candidate set the model will be built on.

        This is the audit hinge: the exact model input is stored alongside the
        query that produced it, so "how did 54,000 rows become these 200
        candidates" always has a re-runnable answer.
        """
        self.con.execute(f"CREATE OR REPLACE TABLE {_quote(table)} AS {query}")
        self.set_meta(**{f"{table}_query": query, f"{table}_rows": self.row_count(table)})
        return self.sql(f"SELECT * FROM {_quote(table)}")

    def record_solution(self, frame: pd.DataFrame, table: str = "solution") -> None:
        if "row_id" not in frame.columns:
            raise ValueError("solution frame must carry row_id so it can join back to source")
        self.con.register("__riskon_solution", frame)
        self.con.execute(
            f"CREATE OR REPLACE TABLE {_quote(table)} AS SELECT * FROM __riskon_solution"
        )
        self.con.unregister("__riskon_solution")

    def record_constraints(self, log: ConstraintLog | pd.DataFrame) -> None:
        frame = log.to_frame() if isinstance(log, ConstraintLog) else log
        self.con.register("__riskon_constraints", frame)
        self.con.execute(
            "CREATE OR REPLACE TABLE constraints AS SELECT * FROM __riskon_constraints"
        )
        self.con.unregister("__riskon_constraints")

    def record(
        self,
        solution: pd.DataFrame | None = None,
        constraints: ConstraintLog | pd.DataFrame | None = None,
        **meta: Any,
    ) -> None:
        """Write solver output back into the artifact in one call."""
        if solution is not None:
            self.record_solution(solution)
        if constraints is not None:
            self.record_constraints(constraints)
        if meta:
            self.set_meta(**meta)

    # -- meta --------------------------------------------------------------

    def set_meta(self, **items: Any) -> None:
        now = datetime.now(timezone.utc)
        for key, value in items.items():
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                value = json.dumps(value, default=str)
            self.con.execute(
                """
                INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                                updated_at = excluded.updated_at
                """,
                [str(key), None if value is None else str(value), now],
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return default if row is None else row[0]

    def all_meta(self) -> dict[str, str]:
        rows = self.con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return {key: value for key, value in rows}

    def add_assumption(self, text: str) -> None:
        """Append to the assumption ledger: any number the agent invented."""
        current = json.loads(self.get_meta("assumptions", "[]") or "[]")
        if text not in current:
            current.append(text)
        self.set_meta(assumptions=current)

    def assumptions(self) -> list[str]:
        return json.loads(self.get_meta("assumptions", "[]") or "[]")

    # -- export ------------------------------------------------------------

    def export(self, destination: Path, fmt: str = "csv") -> list[Path]:
        """The one place another format appears - an exit, not a way station."""
        destination.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for table in self.tables():
            target = destination / f"{table}.{fmt}"
            literal = str(target).replace("'", "''")
            if fmt == "parquet":
                self.con.execute(f"COPY {_quote(table)} TO '{literal}' (FORMAT PARQUET)")
            else:
                self.con.execute(f"COPY {_quote(table)} TO '{literal}' (HEADER, DELIMITER ',')")
            written.append(target)
        return written


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def workbench_path(run_dir: Path | str | None = None) -> Path | None:
    run = Path(run_dir) if run_dir is not None else current_run()
    return None if run is None else Path(run) / WORKBENCH_NAME


def connect(run_dir: Path | str | None = None, read_only: bool = False) -> Workbench:
    """Open the run's workbench, or an in-memory scratch database if none is active."""
    path = workbench_path(run_dir)
    return Workbench(path, read_only=read_only)


def open_run(run_dir: Path | str) -> Workbench:
    """Open a specific run's artifact - used by reports and verification."""
    return Workbench(Path(run_dir) / WORKBENCH_NAME)


def md_table(frame: pd.DataFrame, limit: int | None = None) -> str:
    """Render a DataFrame as a markdown table for reports and CLI output."""
    view = frame if limit is None else frame.head(limit)
    return view.to_markdown(index=False)


def as_records(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
