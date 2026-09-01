"""Staff scheduling: who works which shift.

DECISIONS
    For each (worker, shift) pair, roster them or don't (binary).

OBJECTIVE
    Minimise total staffing cost.

CONSTRAINTS
    1. Every shift must be covered by at least the required headcount.
    2. A worker cannot be on two overlapping shifts.
    3. No worker may exceed MAX_SHIFTS_PER_WORKER in the period.
    4. A worker needs MIN_REST_HOURS between consecutive shifts.

----------------------------------------------------------------------------
This is a TEMPLATE. Unlike the other three it builds its own demonstration
roster, because none of the bundled datasets is a staffing problem. Replace
`build_candidates` with a query over your real shift table; keep the shape.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
from ortools.sat.python import cp_model

from riskon import ConstraintLog, connect, md_table, paths

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

N_WORKERS = 10
MAX_SHIFTS_PER_WORKER = 5
MIN_REST_HOURS = 8
HOURLY_COST = 32.0

PERIOD_START = datetime(2026, 3, 2, 6, 0)
DAYS = 5
SHIFTS_PER_DAY = [
    ("early", 6, 8, 2),    # label, start hour, length hours, required headcount
    ("late", 14, 8, 3),
    ("night", 22, 8, 1),
]


# ---------------------------------------------------------------------------
# 1-2. Build the shift table
# ---------------------------------------------------------------------------


def build_candidates(wb) -> pd.DataFrame:
    rows = []
    for day in range(DAYS):
        for label, hour, length, required in SHIFTS_PER_DAY:
            start = PERIOD_START.replace(hour=0) + timedelta(days=day, hours=hour)
            rows.append(
                {
                    "shift": f"{start:%a}-{label}",
                    "start_ts": start,
                    "end_ts": start + timedelta(hours=length),
                    "hours": length,
                    "required": required,
                }
            )
    frame = pd.DataFrame(rows)

    if not wb.has_table("source"):
        wb.load(frame, table="source")

    wb.add_assumption(
        f"Roster generated for demonstration: {DAYS} days x {len(SHIFTS_PER_DAY)} "
        f"shifts, {N_WORKERS} interchangeable workers at {HOURLY_COST:g}/hour."
    )

    return wb.materialize(
        """
        SELECT row_id, shift, start_ts, end_ts, hours, required,
               epoch(start_ts)::BIGINT AS start_s,
               epoch(end_ts)::BIGINT   AS end_s
        FROM source
        ORDER BY start_ts
        """
    )


# ---------------------------------------------------------------------------
# 3-5. Model and solve
# ---------------------------------------------------------------------------


def solve(shifts: pd.DataFrame, log: ConstraintLog):
    model = cp_model.CpModel()

    n = len(shifts)
    workers = range(N_WORKERS)
    start = shifts["start_s"].to_numpy(dtype=int)
    end = shifts["end_s"].to_numpy(dtype=int)
    hours = shifts["hours"].to_numpy(dtype=int)
    required = shifts["required"].to_numpy(dtype=int)

    x = {(s, w): model.NewBoolVar(f"x[{s},{w}]") for s in range(n) for w in workers}

    # 1. Coverage.
    for s in range(n):
        model.Add(sum(x[s, w] for w in workers) >= int(required[s]))
    log.add(
        name="coverage",
        business_rule="Every shift must be staffed to its required headcount",
        expression="sum_w x[s,w] >= required_s for every shift s",
        sense=">=",
        bound=0.0,
    )

    # 2. No overlap per worker.
    for w in workers:
        model.AddNoOverlap(
            [
                model.NewOptionalIntervalVar(
                    int(start[s]), int(end[s] - start[s]), int(end[s]), x[s, w], f"iv[{s},{w}]"
                )
                for s in range(n)
            ]
        )
    log.add(
        name="no_overlap",
        business_rule="A worker cannot be rostered on two overlapping shifts",
        expression="AddNoOverlap over each worker's assigned shift intervals",
        sense="<=",
        bound=0.0,
    )

    # 3. Workload cap.
    for w in workers:
        model.Add(sum(x[s, w] for s in range(n)) <= MAX_SHIFTS_PER_WORKER)
    log.add(
        name="max_shifts",
        business_rule=f"No worker may work more than {MAX_SHIFTS_PER_WORKER} shifts",
        expression=f"sum_s x[s,w] <= {MAX_SHIFTS_PER_WORKER} for every worker w",
        sense="<=",
        bound=float(MAX_SHIFTS_PER_WORKER),
    )

    # 4. Rest between shifts: forbid pairs that are too close together.
    rest_seconds = MIN_REST_HOURS * 3600
    for a in range(n):
        for b in range(a + 1, n):
            gap = int(start[b]) - int(end[a])
            if 0 <= gap < rest_seconds:
                for w in workers:
                    model.Add(x[a, w] + x[b, w] <= 1)
    log.add(
        name="rest",
        business_rule=f"A worker needs {MIN_REST_HOURS} hours off between shifts",
        expression=f"x[a,w] + x[b,w] <= 1 whenever gap(a,b) < {MIN_REST_HOURS}h",
        sense=">=",
        bound=float(MIN_REST_HOURS),
    )

    model.Minimize(
        sum(int(hours[s] * HOURLY_COST) * x[s, w] for s in range(n) for w in workers)
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    solver.parameters.num_search_workers = 8

    started = time.perf_counter()
    status = solver.Solve(model)
    runtime = time.perf_counter() - started
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status_name, runtime, None

    records = []
    for s in range(n):
        for w in workers:
            if solver.Value(x[s, w]):
                records.append(
                    {
                        "row_id": int(shifts.loc[s, "row_id"]),
                        "shift": shifts.loc[s, "shift"],
                        "worker": w,
                        "start_ts": shifts.loc[s, "start_ts"],
                        "end_ts": shifts.loc[s, "end_ts"],
                        "hours": int(hours[s]),
                        "selected": 1,
                    }
                )

    return pd.DataFrame(records), status_name, runtime, float(solver.ObjectiveValue())


# ---------------------------------------------------------------------------
# 6. Verify independently
# ---------------------------------------------------------------------------


def verify(shifts: pd.DataFrame, solution: pd.DataFrame, log: ConstraintLog) -> pd.DataFrame:
    staffed = solution.groupby("row_id").size().rename("staffed")
    check = shifts.set_index("row_id").join(staffed).fillna({"staffed": 0})
    shortfall = (check["required"] - check["staffed"]).max()
    log.set_achieved("coverage", float(-shortfall))

    overlaps = 0
    min_gap_hours = float("inf")
    for _, jobs in solution.groupby("worker"):
        ordered = jobs.sort_values("start_ts").reset_index(drop=True)
        for k in range(len(ordered) - 1):
            gap = (ordered.loc[k + 1, "start_ts"] - ordered.loc[k, "end_ts"]).total_seconds()
            if gap < 0:
                overlaps += 1
            min_gap_hours = min(min_gap_hours, gap / 3600)
    log.set_achieved("no_overlap", float(overlaps))
    log.set_achieved("rest", 0.0 if min_gap_hours == float("inf") else float(min_gap_hours))
    log.set_achieved("max_shifts", float(solution.groupby("worker").size().max()))

    violations = log.violations()
    if violations:
        raise AssertionError(
            "solution violates constraints that were supposed to hold: "
            + ", ".join(f"{c.name} ({c.achieved} vs {c.sense} {c.bound})" for c in violations)
        )
    return check.reset_index()


# ---------------------------------------------------------------------------


def main() -> int:
    wb = connect()
    log = ConstraintLog()

    shifts = build_candidates(wb)
    print(f"shifts: {len(shifts)} x {N_WORKERS} workers")

    solution, status, runtime, objective = solve(shifts, log)
    print(f"status: {status} in {runtime:.3f}s")

    if solution is None:
        wb.record(constraints=log, status=status, runtime_seconds=runtime)
        print("\nINFEASIBLE. Usual culprits: too few workers for the coverage")
        print("requirement, or MIN_REST_HOURS conflicting with back-to-back shifts.")
        wb.close()
        return 1

    coverage = verify(shifts, solution, log)
    wb.record(
        solution=solution,
        constraints=log,
        status=status,
        objective=objective,
        runtime_seconds=runtime,
        solver="ortools.cp_model",
        model="scheduling_cpsat",
    )

    roster = (
        solution.groupby("worker")
        .agg(shifts_worked=("shift", "count"), hours=("hours", "sum"))
        .reset_index()
        .rename(columns={"worker": "Worker", "shifts_worked": "Shifts", "hours": "Hours"})
    )

    report = "\n".join(
        [
            "# Staffing roster recommendation",
            "",
            "## Recommendation",
            "",
            f"Roster **{len(solution)} worker-shifts** across "
            f"{solution['worker'].nunique()} workers at a total cost of "
            f"**{objective:,.0f}**, covering every shift.",
            "",
            "## The decision",
            "",
            md_table(roster),
            "",
            "## Coverage",
            "",
            md_table(coverage[["shift", "required", "staffed"]].rename(
                columns={"shift": "Shift", "required": "Required", "staffed": "Staffed"}
            )),
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
            *[f"- {a}" for a in wb.assumptions()],
        ]
    )

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
