"""End-to-end smoke test of the database-to-solver path.

Covers the real route a run takes: load a file into a canonical table, narrow
it with SQL, solve, verify, then reopen the artifact in a fresh connection and
confirm it is self-contained.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from riskon import ConstraintLog, Workbench, connect, paths
from riskon.db import canonical_columns, snake_case

REPO = Path(__file__).resolve().parents[1]
MPG = REPO / "data" / "mpg.csv"


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def test_snake_case_handles_the_shapes_that_actually_occur():
    assert snake_case("Miles per Gallon") == "miles_per_gallon"
    assert snake_case("unitPrice") == "unit_price"
    assert snake_case("  Total $ ") == "total"
    assert snake_case("2024") == "col_2024"


def test_duplicate_columns_are_disambiguated():
    assert canonical_columns(["Price", "price", "PRICE"]) == ["price", "price_1", "price_2"]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def test_load_produces_a_canonical_table(tmp_path):
    with Workbench(tmp_path / "workbench.duckdb") as wb:
        wb.load(MPG)

        columns = wb.columns("source")
        assert columns[0] == "row_id"
        assert "model_year" in columns
        assert all(c == c.lower() for c in columns)
        assert wb.row_count("source") == 398

        # row_id is a usable key, not decoration.
        distinct = wb.sql("SELECT count(DISTINCT row_id) AS n FROM source")["n"][0]
        assert distinct == 398

        # Types are resolved, not left as strings.
        types = dict(wb.sql("DESCRIBE source")[["column_name", "column_type"]].values)
        assert types["mpg"] in {"DOUBLE", "FLOAT"}
        assert types["origin"] == "VARCHAR"


def test_load_round_trips_a_dataframe_and_other_formats(tmp_path):
    import pandas as pd

    frame = pd.DataFrame({"Item Name": ["a", "b"], "Unit Cost": [1.5, 2.5]})
    parquet = tmp_path / "inbound.parquet"
    frame.to_parquet(parquet)

    with Workbench(tmp_path / "workbench.duckdb") as wb:
        wb.load(frame, table="from_frame")
        wb.load(parquet, table="from_parquet")

        assert wb.columns("from_frame") == ["row_id", "item_name", "unit_cost"]
        assert wb.columns("from_parquet") == ["row_id", "item_name", "unit_cost"]
        assert wb.row_count("from_parquet") == 2


def test_profile_reports_nulls_and_categories(tmp_path):
    with Workbench(tmp_path / "workbench.duckdb") as wb:
        wb.load(MPG)
        text = wb.profile("source")

    assert "398" in text
    assert "horsepower" in text
    # mpg.csv has a genuinely categorical origin column; the profile must
    # surface its values, since that is what constraints get written against.
    assert "origin" in text
    assert "usa" in text


def test_materialize_records_the_query(tmp_path):
    with Workbench(tmp_path / "workbench.duckdb") as wb:
        wb.load(MPG)
        candidates = wb.materialize(
            "SELECT row_id, name, horsepower FROM source WHERE horsepower IS NOT NULL"
        )

        assert len(candidates) == 392  # 6 rows have a null horsepower
        assert "horsepower IS NOT NULL" in wb.get_meta("candidates_query")


# ---------------------------------------------------------------------------
# Constraint logging
# ---------------------------------------------------------------------------


def test_constraint_slack_and_binding():
    log = ConstraintLog()
    log.add("budget", "Stay within budget", "sum(c_i x_i) <= 100", "<=", 100)
    log.set_achieved("budget", 90)
    budget = log.items[0]
    assert budget.slack == pytest.approx(10)
    assert budget.binding is False
    assert budget.satisfied is True

    log.set_achieved("budget", 100)
    assert log.items[0].binding is True

    log.set_achieved("budget", 110)
    assert log.items[0].satisfied is False
    assert len(log.violations()) == 1


def test_lower_bound_constraints_use_the_right_sign():
    log = ConstraintLog()
    log.add("mpg", "Average at least 25 mpg", "avg(mpg) >= 25", ">=", 25)
    log.set_achieved("mpg", 27)
    assert log.items[0].slack == pytest.approx(2)
    assert log.items[0].satisfied is True

    log.set_achieved("mpg", 24)
    assert log.items[0].satisfied is False


# ---------------------------------------------------------------------------
# The real path: solve a model and read back the artifact
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """An isolated run, so tests never touch the repo's runs/ directory."""
    monkeypatch.setenv("RISKON_HOME", str(REPO))
    monkeypatch.setenv("RISKON_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("RISKON_RUN", str(tmp_path / "runs" / "test-run"))
    (tmp_path / "runs" / "test-run").mkdir(parents=True)
    monkeypatch.chdir(REPO)
    return tmp_path / "runs" / "test-run"


def test_selection_milp_end_to_end(run_dir):
    assert paths.current_run() == run_dir
    template = REPO / "templates" / "selection_milp.py"

    completed = subprocess.run(
        [sys.executable, str(template)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OPTIMAL" in completed.stdout

    # Reopen in a fresh connection: this is the real test of the artifact claim.
    with Workbench(run_dir / "workbench.duckdb") as wb:
        tables = set(wb.tables())
        assert {"source", "candidates", "solution", "constraints", "meta"} <= tables

        # solution joins back to source through row_id alone.
        chosen = wb.sql(
            """
            SELECT s.row_id, src.name, src.weight * 8 AS unit_cost, src.mpg
            FROM solution s
            JOIN source src USING (row_id)
            WHERE s.selected = 1
            """
        )
        assert len(chosen) > 0

        # Recompute the headline constraints straight from source.
        assert float(chosen["unit_cost"].sum()) <= 250_000 + 1e-6
        assert float(chosen["mpg"].mean()) >= 25 - 1e-6
        assert len(chosen) <= 10

        constraints = wb.sql("SELECT * FROM constraints")
        assert not constraints.empty
        assert constraints["satisfied"].all()
        # Every constraint carries the sentence it came from.
        assert constraints["business_rule"].str.len().gt(0).all()

        assert wb.get_meta("status") == "OPTIMAL"
        assert len(wb.assumptions()) >= 1

    assert (run_dir / "report.md").exists()
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Recommendation" in report
    assert "## Assumptions" in report

    # The walkthrough is the file the stakeholder's "How we got here" tab
    # opens. A report without it is a number they have to take on faith.
    assert (run_dir / "walkthrough.md").exists()
    walkthrough = (run_dir / "walkthrough.md").read_text(encoding="utf-8")
    assert "## How we turned your question into a search" in walkthrough


def test_export_writes_csv(run_dir):
    with Workbench(run_dir / "workbench.duckdb") as wb:
        wb.load(MPG)
        written = wb.export(run_dir / "export", fmt="csv")
    assert any(p.name == "source.csv" for p in written)
    assert (run_dir / "export" / "source.csv").stat().st_size > 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_doctor_reports_a_healthy_stack():
    completed = subprocess.run(
        [sys.executable, "-m", "riskon.cli", "doctor"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout
    assert "all systems go" in completed.stdout


def test_cli_load_and_sql(run_dir):
    load = subprocess.run(
        [sys.executable, "-m", "riskon.cli", "load", str(MPG)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert load.returncode == 0, load.stdout + load.stderr
    assert "398" in load.stdout

    query = subprocess.run(
        [sys.executable, "-m", "riskon.cli", "sql", "SELECT count(*) AS n FROM source"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert query.returncode == 0, query.stdout + query.stderr
    assert "398" in query.stdout


def test_cli_publish_refuses_a_missing_walkthrough(run_dir, tmp_path):
    (run_dir / "report.md").write_text("# Recommendation\nBuy ten.\n", encoding="utf-8")
    destination = tmp_path / "store"

    completed = subprocess.run(
        [sys.executable, "-m", "riskon.cli", "publish", "--into", str(destination)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "walkthrough.md is missing" in completed.stderr
    # The report still lands, so the stakeholder is not empty-handed.
    assert (destination / "report.md").exists()
    assert not (destination / "walkthrough.md").exists()


def test_cli_publish_copies_the_walkthrough(run_dir, tmp_path):
    (run_dir / "report.md").write_text("# Recommendation\nBuy ten.\n", encoding="utf-8")
    (run_dir / "walkthrough.md").write_text(
        "# How we got here\n\n## How we turned your question into a search\n",
        encoding="utf-8",
    )
    destination = tmp_path / "store"

    completed = subprocess.run(
        [sys.executable, "-m", "riskon.cli", "publish", "--into", str(destination)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (destination / "walkthrough.md").exists()
    assert "walkthrough.md" in completed.stdout


def test_connect_without_a_run_is_in_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("RISKON_RUN_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("RISKON_RUN", raising=False)
    with connect() as wb:
        assert str(wb.path) == ":memory:"
