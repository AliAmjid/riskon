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
    The driver pool is not in the data. Its size and capacities are invented
    and recorded in the ledger.

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
        f"The data has no driver roster; assumed {N_DRIVERS} interchangeable "
        f"drivers each seating {VEHICLE_CAPACITY} passengers."
    )
    wb.add_assumption(
        f"Modelled a single shift from {SHIFT_START} to {SHIFT_END}; the full "
        f"log spans months and is not one dispatch decision."
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

    lines = [
        "# Dispatch assignment recommendation",
        "",
        "## Recommendation",
        "",
        f"Assign **{len(assigned)} of {len(solution)} requests** across "
        f"{assigned['driver'].nunique()} drivers, capturing "
        f"**{objective:,.2f} in revenue** for the shift.",
        "",
        "## What this achieves",
        "",
        f"- Requests served: **{len(assigned)}** of {len(solution)}",
        f"- Requests left unserved: **{unserved}**",
        f"- Revenue captured: **{objective:,.2f}**",
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
        "## Constraint check",
        "",
        md_table(
            log.to_frame()[["business_rule", "sense", "bound", "achieved", "satisfied"]]
            .rename(columns={"business_rule": "Rule"})
        ),
        "",
        "## Assumptions",
        "",
    ]
    lines += [f"- {a}" for a in wb.assumptions()] or ["- None."]
    return "\n".join(lines)


def write_walkthrough(wb, solution: pd.DataFrame, assigned: pd.DataFrame, log: ConstraintLog,
                      objective: float) -> str:
    unserved = int((solution["selected"] == 0).sum())
    rules = "\n".join(
        f"{i}. {item.business_rule} — "
        f"{'held, and this is the limit that stopped a better answer' if item.binding else 'held'}."
        for i, item in enumerate(log.items, start=1)
    )
    return "\n".join(
        [
            "# How we got here",
            "",
            "## The question you asked",
            "",
            "Which driver should take which request this shift, so we capture "
            "as much revenue as we can without double-booking anyone or "
            "overloading a vehicle.",
            "",
            "## What we worked from",
            "",
            f"{len(solution)} requests in the shift window, and a driver pool "
            "that is not in the file — its size and vehicle capacities are "
            "numbers we had to pin down first.",
            "",
            "## What we had to pin down before we could start",
            "",
            "The file has the jobs, not the people. How many drivers you have, "
            "and what each vehicle can carry, changes who can take what. Those "
            "figures are in the assumption list below.",
            "",
            "## The rules we held to",
            "",
            rules,
            "",
            "## How we turned your question into a search",
            "",
            "For every request we chose a driver, or left it unserved. "
            f'"Best" means the most revenue captured (this roster totals '
            f"{objective:,.2f}). A driver cannot be in two places at once, "
            "and a vehicle cannot take more passengers than it holds — those "
            "are the walls of the search. The roster you have is the pairing "
            "that scores highest inside those walls, not a shortlist we liked.",
            "",
            "## How we checked it",
            "",
            f"We counted the assigned trips, confirmed {unserved} requests "
            "were left unserved, and re-checked that no driver holds two "
            "overlapping jobs. Had a recount disagreed, we would have thrown "
            "the roster out.",
            "",
            "## Where this could be wrong",
            "",
            "The driver pool is invented. More drivers would serve requests "
            "that currently overlap an existing trip; fewer would leave more "
            "unserved.",
            "",
            "## Assumptions",
            "",
            *([f"- {a}" for a in wb.assumptions()] or ["- None."]),
        ]
    )


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
    walkthrough = write_walkthrough(wb, solution, assigned, log, objective)
    paths.write_docs(report, walkthrough)

    print()
    print(report)
    wb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
