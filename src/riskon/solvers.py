"""Solver inventory and the selection cheatsheet.

``doctor`` proves every backend actually instantiates; ``cheatsheet`` is the
same problem-class-to-backend mapping that AGENTS.md carries, available from
the shell so the agent never has to open a file to choose.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

CHEATSHEET = """\
Pick the backend by problem class, not by preference:

  Pick a subset under budget / ratio caps  ->  OR-Tools pywraplp + SCIP or HIGHS
      Binary or integer selection. Track A (fleet) and Track B (vault) are both
      this shape. from ortools.linear_solver import pywraplp

  Assignment, matching, no-overlap scheduling  ->  OR-Tools CP-SAT
      Discrete combinatorial structure, interval variables, AddNoOverlap.
      Track C (dispatch) is this shape.
      from ortools.sat.python import cp_model

  Pure continuous blend / allocation  ->  HiGHS via scipy.optimize.linprog
      No integrality anywhere. Fastest path, least ceremony.

  Quadratic risk, diversification, portfolio variance  ->  cvxpy
      Objective or constraints are convex but not linear.

  Quick prototype, or a second opinion on a suspicious result  ->  pulp
      Bundles CBC. Useful for cross-checking an OR-Tools answer.

  Flow, matching, shortest path, connectivity checks  ->  networkx
      Also good for sanity-checking that a graph model is even feasible.

  Algebraic modelling when the OR-Tools API is fighting you  ->  Pyomo
      Standalone cbc and glpsol binaries are installed in the image.

  Gurobi is installed but ships a size-limited pip licence (~2000 vars and
  2000 constraints). Fine for a demo, will fail on a full-size model, so do
  not reach for it first.
"""


@dataclass
class BackendStatus:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"  [{mark}] {self.name:<28} {self.detail}"


def _version(module_name: str, attr: str = "__version__") -> str:
    module = importlib.import_module(module_name)
    return str(getattr(module, attr, "unknown"))


def check_libraries() -> list[BackendStatus]:
    results: list[BackendStatus] = []
    for label, module in [
        ("duckdb", "duckdb"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("pyarrow", "pyarrow"),
        ("polars", "polars"),
        ("scipy", "scipy"),
        ("ortools", "ortools"),
        ("pulp", "pulp"),
        ("cvxpy", "cvxpy"),
        ("pyomo", "pyomo"),
        ("gurobipy", "gurobipy"),
        ("networkx", "networkx"),
        ("matplotlib", "matplotlib"),
    ]:
        try:
            results.append(BackendStatus(label, True, _version(module)))
        except Exception as exc:  # pragma: no cover - image build catches this
            results.append(BackendStatus(label, False, f"{type(exc).__name__}: {exc}"))
    return results


def check_mip_backends() -> list[BackendStatus]:
    results: list[BackendStatus] = []
    try:
        from ortools.linear_solver import pywraplp

        for name in ("SCIP", "CBC", "GLOP", "HIGHS"):
            solver = pywraplp.Solver.CreateSolver(name)
            results.append(
                BackendStatus(
                    f"pywraplp:{name}",
                    solver is not None,
                    "ready" if solver is not None else "unavailable",
                )
            )
    except Exception as exc:  # pragma: no cover
        results.append(BackendStatus("pywraplp", False, f"{type(exc).__name__}: {exc}"))

    try:
        from ortools.sat.python import cp_model

        model = cp_model.CpModel()
        x = model.NewBoolVar("x")
        model.Add(x == 1)
        status = cp_model.CpSolver().Solve(model)
        ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        results.append(BackendStatus("cp-sat", ok, "solves a trivial model"))
    except Exception as exc:  # pragma: no cover
        results.append(BackendStatus("cp-sat", False, f"{type(exc).__name__}: {exc}"))

    try:
        import numpy as np
        from scipy.optimize import linprog

        res = linprog(c=[-1.0], A_ub=np.array([[1.0]]), b_ub=[3.0], bounds=[(0, None)])
        results.append(BackendStatus("scipy:highs", bool(res.success), "solves a trivial LP"))
    except Exception as exc:  # pragma: no cover
        results.append(BackendStatus("scipy:highs", False, f"{type(exc).__name__}: {exc}"))

    return results


def check_conflicts() -> list[BackendStatus]:
    """Catch known package conflicts with an actionable message.

    highspy is the one that matters: cvxpy depends on it, so a plain
    ``pip install -e .`` drags it back in, and its libhighs symbols then break
    every OR-Tools import. Without this check the failure surfaces as an
    undefined-symbol ImportError that says nothing about the cause.
    """
    try:
        importlib.import_module("highspy")
    except ImportError:
        return [BackendStatus("no highspy conflict", True, "clear")]

    return [
        BackendStatus(
            "highspy conflict",
            False,
            "highspy is installed and WILL break OR-Tools. Fix:\n"
            "         python3 -m pip uninstall -y --break-system-packages highspy\n"
            "         (it arrives as a cvxpy dependency; install this package\n"
            "          with --no-deps to avoid pulling it back in)",
        )
    ]


def check_workbench() -> list[BackendStatus]:
    try:
        import duckdb
        import pandas as pd

        con = duckdb.connect()
        con.register("probe", pd.DataFrame({"Miles per Gallon": [1, 2]}))
        value = con.sql("SELECT count(*) FROM probe").fetchone()[0]
        con.close()
        return [BackendStatus("duckdb:workbench", value == 2, "in-memory round-trip")]
    except Exception as exc:  # pragma: no cover
        return [BackendStatus("duckdb:workbench", False, f"{type(exc).__name__}: {exc}")]


def doctor() -> tuple[str, bool]:
    """Return a human-readable report and whether everything passed."""
    # Conflicts run first: a highspy clash makes every OR-Tools check below it
    # fail with an unreadable symbol error, so name the cause before the symptom.
    sections = [
        ("Conflicts", check_conflicts()),
        ("Libraries", check_libraries()),
        ("MIP / CP backends", check_mip_backends()),
        ("Data workbench", check_workbench()),
    ]

    lines = ["riskon doctor", ""]
    healthy = True
    for title, statuses in sections:
        lines.append(f"{title}:")
        for status in statuses:
            lines.append(status.line())
            healthy = healthy and status.ok
        lines.append("")

    lines.append("all systems go" if healthy else "SOME CHECKS FAILED - see above")
    return "\n".join(lines), healthy
