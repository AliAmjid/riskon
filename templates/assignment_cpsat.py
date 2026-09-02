"""Dispatch assignment: which driver takes which job.

DECISIONS
    For each (task, driver) pair, assign it or don't (binary).

OBJECTIVE
    Maximise total platform revenue over assigned tasks.

CONSTRAINTS
    1. Each task goes to at most one driver.
    2. No driver may hold two overlapping tasks.
    3. A task's passenger count must fit the driver's vehicle capacity.

ASSUMPTIONS
    The driver pool is not in the data. Demo driver count, vehicle capacity and
    shift window are recorded as GUESSED and surfaced in the report.

----------------------------------------------------------------------------
This is a TEMPLATE. It runs as-is against data/taxis.csv. Edit CONFIG and the
constraint block for the real question. CP-SAT is the right backend here
because the structure is combinatorial: overlap is a relation between pairs of
tasks, not a linear inequality on totals.
"""

from __future__ import annotations

import time

import pandas as pd
from ortools.sat.python import cp_model

from riskon import ConstraintLog, connect, md_table, paths

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = "data/taxis.csv"

SHIFT_START = "2019-03-23 19:00:00"   # one operational slice, not the whole log
SHIFT_END = "2019-03-23 21:00:00"
MAX_TASKS = 120                       # keep the pairwise overlap check sane

N_DRIVERS = 8
VEHICLE_CAPACITY = 4

REVENUE_COLUMN = "total"
START_COLUMN = "pickup"
END_COLUMN = "dropoff"
PASSENGERS_COLUMN = "passengers"


# ---------------------------------------------------------------------------
# 1-2. Data -> candidate set
# ---------------------------------------------------------------------------


def build_candidates(wb) -> pd.DataFrame:
    if not wb.has_table("source"):
        wb.load(DEFAULT_SOURCE)

    wb.add_assumption(
        f"GUESSED: the data has no driver roster; assumed {N_DRIVERS} "
        f"interchangeable drivers."
    )
    wb.add_assumption(
        f"GUESSED: each driver has a {VEHICLE_CAPACITY}-passenger vehicle; "
        "replace with the actual vehicle capacities before using operationally."
    )
    wb.add_assumption(
        f"GUESSED: modelled a single shift from {SHIFT_START} to {SHIFT_END}; "
        "the full log spans months and is not one dispatch decision."
    )
    wb.add_assumption(
        f"GUESSED: limited the demo to the first {MAX_TASKS} qualifying "
        "requests in the shift window to keep the overlap check small."
    )

    return wb.materialize(
        f"""
        SELECT
            row_id,
            {START_COLUMN}  AS start_ts,
            {END_COLUMN}    AS end_ts,
            {PASSENGERS_COLUMN} AS passengers,
            {REVENUE_COLUMN} AS revenue,
            pickup_zone, dropoff_zone,
            epoch({START_COLUMN})::BIGINT AS start_s,
            epoch({END_COLUMN})::BIGINT   AS end_s
        FROM source
        WHERE {START_COLUMN} >= TIMESTAMP '{SHIFT_START}'
          AND {END_COLUMN}   <= TIMESTAMP '{SHIFT_END}'
          AND {REVENUE_COLUMN} IS NOT NULL
          AND {PASSENGERS_COLUMN} IS NOT NULL
          AND {END_COLUMN} > {START_COLUMN}
        ORDER BY {START_COLUMN}
        LIMIT {MAX_TASKS}
        """
    )


# ---------------------------------------------------------------------------
# 3-5. Model and solve
# ---------------------------------------------------------------------------


def solve(candidates: pd.DataFrame, log: ConstraintLog):
    model = cp_model.CpModel()

    n = len(candidates)
    drivers = range(N_DRIVERS)
    revenue = candidates["revenue"].to_numpy(dtype=float)
    start = candidates["start_s"].to_numpy(dtype=int)
    end = candidates["end_s"].to_numpy(dtype=int)
    passengers = candidates["passengers"].to_numpy(dtype=int)

    # Revenue is currency with cents; CP-SAT is integral, so work in cents.
    revenue_cents = [int(round(r * 100)) for r in revenue]

    x = {(i, d): model.NewBoolVar(f"x[{i},{d}]") for i in range(n) for d in drivers}

    # 1. Each task assigned at most once.
    for i in range(n):
        model.AddAtMostOne(x[i, d] for d in drivers)
    log.add(
        name="single_assignment",
        business_rule="Each customer request is served by at most one driver",
        expression="sum_d x[i,d] <= 1 for every task i",
        sense="<=",
        bound=1.0,
    )

    # 2. No overlapping work per driver. Optional intervals make this exact:
    #    an interval only exists when its assignment variable is true.
    for d in drivers:
        intervals = [
            model.NewOptionalIntervalVar(
                int(start[i]), int(end[i] - start[i]), int(end[i]), x[i, d], f"iv[{i},{d}]"
            )
            for i in range(n)
        ]
        model.AddNoOverlap(intervals)
    log.add(
        name="no_overlap",
        business_rule="A driver cannot be on two trips at the same time",
        expression="AddNoOverlap over each driver's assigned intervals",
        sense="<=",
        bound=0.0,
    )

    # 3. Capacity.
    for i in range(n):
        if passengers[i] > VEHICLE_CAPACITY:
            for d in drivers:
                model.Add(x[i, d] == 0)
    log.add(
        name="capacity",
        business_rule=f"A trip's passengers must fit the {VEHICLE_CAPACITY}-seat vehicle",
        expression=f"passengers_i <= {VEHICLE_CAPACITY} for any assigned task",
        sense="<=",
        bound=float(VEHICLE_CAPACITY),
    )

    model.Maximize(sum(revenue_cents[i] * x[i, d] for i in range(n) for d in drivers))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    solver.parameters.num_search_workers = 8

    started = time.perf_counter()
    status = solver.Solve(model)
    runtime = time.perf_counter() - started

    status_name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status_name, runtime, None

    assigned_to = []
    for i in range(n):
        driver = next((d for d in drivers if solver.Value(x[i, d])), None)
        assigned_to.append(driver)

    solution = pd.DataFrame(
        {
            "row_id": candidates["row_id"].to_numpy(),
            "driver": assigned_to,
            "selected": [int(d is not None) for d in assigned_to],
            "revenue": revenue,
            "passengers": passengers,
            "start_ts": candidates["start_ts"].to_numpy(),
            "end_ts": candidates["end_ts"].to_numpy(),
            "pickup_zone": candidates["pickup_zone"].to_numpy(),
            "dropoff_zone": candidates["dropoff_zone"].to_numpy(),
        }
    )
    return solution, status_name, runtime, solver.ObjectiveValue() / 100.0


# ---------------------------------------------------------------------------
# 6. Verify independently
# ---------------------------------------------------------------------------


def verify(solution: pd.DataFrame, log: ConstraintLog) -> pd.DataFrame:
    assigned = solution[solution["selected"] == 1].copy()
    if assigned.empty:
        raise AssertionError("solver assigned no tasks at all")

    # Re-derive overlap in pandas rather than trusting AddNoOverlap.
    worst_overlap = 0
    for _, jobs in assigned.groupby("driver"):
        ordered = jobs.sort_values("start_ts").reset_index(drop=True)
        for k in range(len(ordered) - 1):
            if ordered.loc[k + 1, "start_ts"] < ordered.loc[k, "end_ts"]:
                worst_overlap += 1

    log.set_achieved("single_assignment", float(solution["selected"].max()))
    log.set_achieved("no_overlap", float(worst_overlap))
    log.set_achieved("capacity", float(assigned["passengers"].max()))

    violations = log.violations()
    if violations:
        raise AssertionError(
            "solution violates constraints that were supposed to hold: "
            + ", ".join(f"{c.name} ({c.achieved} vs {c.sense} {c.bound})" for c in violations)
        )
    return assigned


# ---------------------------------------------------------------------------
# 7. Explain
# ---------------------------------------------------------------------------


def write_report(wb, solution: pd.DataFrame, assigned: pd.DataFrame, log: ConstraintLog,
                 objective: float) -> str:
    per_driver = (
        assigned.groupby("driver")
        .agg(trips=("row_id", "count"), revenue=("revenue", "sum"))
        .reset_index()
        .rename(columns={"driver": "Driver", "trips": "Trips", "revenue": "Revenue"})
    )
    unserved = int((solution["selected"] == 0).sum())
    limit_check = log.to_frame().assign(
        status=lambda d: d["satisfied"].map({True: "Met", False: "Missed"}).fillna(
            "Not checked"
        )
    )[["business_rule", "bound", "achieved", "status"]].rename(
        columns={
            "business_rule": "Rule",
            "bound": "Limit",
            "achieved": "Actual",
            "status": "Status",
        }
    )
    guesses = [a for a in wb.assumptions() if not a.startswith("CONFIRMED:")]

    lines = [
        "# Dispatch assignment recommendation",
        "",
        "## Recommendation",
        "",
        f"Using the demo driver assumptions listed below, assign "
        f"**{len(assigned)} of {len(solution)} requests** across "
        f"{assigned['driver'].nunique()} drivers, capturing "
        f"**{objective:,.2f} currency units in revenue** for the shift.",
        "",
        "## What this achieves",
        "",
        f"- Requests served: **{len(assigned)}** of {len(solution)}",
        f"- Requests left unserved: **{unserved}**",
        f"- Revenue captured: **{objective:,.2f} currency units**",
        "",
        "## The decision",
        "",
        md_table(per_driver),
        "",
        "## Why these and not others",
        "",
        f"{unserved} requests went unserved. With no driver idle at those times, "
        "the limit is fleet size rather than demand - each additional driver "
        "unlocks the requests that currently overlap an existing trip.",
        "",
        "## What would change the answer",
        "",
        "The main levers are the number of available drivers, vehicle capacity "
        "and the shift window. More drivers or larger vehicles can turn some "
        "unserved requests into revenue; changing a setting that already has "
        "room left will not move the recommendation much.",
        "",
        "## How the limits checked out",
        "",
        md_table(limit_check),
        "",
        "## What I had to guess",
        "",
    ]
    lines += [f"- {a}" for a in guesses] or ["- None. The stakeholder confirmed every input."]
    lines += [
        "",
        "## Assumptions",
        "",
    ]
    lines += [f"- {a}" for a in wb.assumptions()] or ["- None."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def main() -> int:
    wb = connect()
    log = ConstraintLog()

    candidates = build_candidates(wb)
    print(f"candidates: {len(candidates):,} tasks x {N_DRIVERS} drivers")
    if candidates.empty:
        print("No tasks in that window - widen SHIFT_START/SHIFT_END.")
        wb.close()
        return 1

    solution, status, runtime, objective = solve(candidates, log)
    print(f"status: {status} in {runtime:.3f}s")

    if solution is None:
        wb.record(constraints=log, status=status, runtime_seconds=runtime)
        print("\nINFEASIBLE. Relax one constraint at a time to find the conflict.")
        wb.close()
        return 1

    assigned = verify(solution, log)
    wb.record(
        solution=solution,
        constraints=log,
        status=status,
        objective=objective,
        runtime_seconds=runtime,
        solver="ortools.cp_model",
        model="assignment_cpsat",
    )

    report = write_report(wb, solution, assigned, log, objective)
    run_dir = paths.current_run()
    if run_dir is not None:
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"report: {run_dir / 'report.md'}")

    print()
    print(report)
    wb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
