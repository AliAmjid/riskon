"""Fleet procurement: which vehicles to buy.

DECISIONS
    For each model in the catalogue, buy it or don't (binary).

OBJECTIVE
    Maximise total engine power (sum of horsepower over selected models).

CONSTRAINTS
    1. Total spend must stay within the capital budget.
    2. The fleet must average at least MIN_AVG_MPG, the sustainability target.
    3. No single country of origin may exceed MAX_ORIGIN_SHARE of the fleet.
    4. Buy at most MAX_ITEMS vehicles.

ASSUMPTIONS
    The catalogue has no price column, so unit cost is derived from weight.
    Recorded in the ledger and surfaced in the report.

----------------------------------------------------------------------------
This is a TEMPLATE. It runs as-is against data/mpg.csv so you can see the whole
loop work, then you edit CONFIG and the constraint block for the real question.
Structure to keep: materialise candidates -> solve -> verify independently ->
record to the artifact -> write the report.
"""

from __future__ import annotations

import time

import pandas as pd
from ortools.linear_solver import pywraplp

from riskon import ConstraintLog, connect, md_table, paths

# ---------------------------------------------------------------------------
# CONFIG - edit this for the real problem
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = "data/mpg.csv"

VALUE_COLUMN = "horsepower"     # maximise the sum of this
COST_COLUMN = "unit_cost"       # derived below
LABEL_COLUMN = "name"           # what the stakeholder calls each row
CATEGORY_COLUMN = "origin"      # the column the diversification rule applies to

BUDGET = 250_000.0
MIN_AVG_MPG = 25.0
MAX_ORIGIN_SHARE = 0.50
MAX_ITEMS = 10

COST_PER_KG = 8.0               # invented: see the assumption ledger


# ---------------------------------------------------------------------------
# 1-2. Data -> candidate set
# ---------------------------------------------------------------------------


def build_candidates(wb) -> pd.DataFrame:
    if not wb.has_table("source"):
        wb.load(DEFAULT_SOURCE)

    wb.add_assumption(
        f"The catalogue has no price column; unit cost is modelled as "
        f"weight * {COST_PER_KG:g} currency units."
    )
    wb.add_assumption(
        f"Rows with a missing {VALUE_COLUMN} are excluded as unpurchasable."
    )

    return wb.materialize(
        f"""
        SELECT
            row_id,
            {LABEL_COLUMN}   AS label,
            {CATEGORY_COLUMN} AS category,
            {VALUE_COLUMN}   AS value,
            weight * {COST_PER_KG} AS {COST_COLUMN},
            mpg
        FROM source
        WHERE {VALUE_COLUMN} IS NOT NULL
          AND weight IS NOT NULL
          AND mpg IS NOT NULL
        """
    )


# ---------------------------------------------------------------------------
# 3-5. Model and solve
# ---------------------------------------------------------------------------


def solve(candidates: pd.DataFrame, log: ConstraintLog):
    solver = pywraplp.Solver.CreateSolver("SCIP") or pywraplp.Solver.CreateSolver("CBC")
    if solver is None:
        raise RuntimeError("no MIP backend available - run `riskon doctor`")

    n = len(candidates)
    x = [solver.BoolVar(f"x[{i}]") for i in range(n)]

    value = candidates["value"].to_numpy(dtype=float)
    cost = candidates[COST_COLUMN].to_numpy(dtype=float)
    mpg = candidates["mpg"].to_numpy(dtype=float)
    category = candidates["category"].to_numpy()

    # 1. Budget
    solver.Add(solver.Sum(cost[i] * x[i] for i in range(n)) <= BUDGET)
    log.add(
        name="budget",
        business_rule=f"Total spend must stay within the {BUDGET:,.0f} capital budget",
        expression=f"sum(unit_cost_i * x_i) <= {BUDGET:,.0f}",
        sense="<=",
        bound=BUDGET,
    )

    # 2. Fleet size
    solver.Add(solver.Sum(x) <= MAX_ITEMS)
    log.add(
        name="fleet_size",
        business_rule=f"Buy at most {MAX_ITEMS} vehicles",
        expression=f"sum(x_i) <= {MAX_ITEMS}",
        sense="<=",
        bound=float(MAX_ITEMS),
    )

    # 3. Sustainability target. "Average mpg >= T" is not linear as written;
    #    multiplying through by the fleet size makes it so.
    solver.Add(solver.Sum((mpg[i] - MIN_AVG_MPG) * x[i] for i in range(n)) >= 0)
    log.add(
        name="avg_mpg",
        business_rule=f"The fleet must average at least {MIN_AVG_MPG:g} mpg",
        expression=f"sum((mpg_i - {MIN_AVG_MPG:g}) * x_i) >= 0",
        sense=">=",
        bound=MIN_AVG_MPG,
    )

    # 4. Diversification: no origin may dominate the fleet.
    for group in sorted(set(category)):
        members = [i for i in range(n) if category[i] == group]
        solver.Add(
            solver.Sum(x[i] for i in members)
            <= MAX_ORIGIN_SHARE * solver.Sum(x)
        )
        log.add(
            name=f"share_{group}",
            business_rule=(
                f"No more than {MAX_ORIGIN_SHARE:.0%} of the fleet may come "
                f"from {group}"
            ),
            expression=f"sum(x_i for i in {group}) <= {MAX_ORIGIN_SHARE} * sum(x_i)",
            sense="<=",
            bound=MAX_ORIGIN_SHARE,
        )

    solver.Maximize(solver.Sum(value[i] * x[i] for i in range(n)))

    started = time.perf_counter()
    status = solver.Solve()
    runtime = time.perf_counter() - started

    status_name = {
        pywraplp.Solver.OPTIMAL: "OPTIMAL",
        pywraplp.Solver.FEASIBLE: "FEASIBLE",
        pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
        pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
        pywraplp.Solver.ABNORMAL: "ABNORMAL",
        pywraplp.Solver.NOT_SOLVED: "NOT_SOLVED",
    }.get(status, str(status))

    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        # Do not quietly relax a bound here. Drop constraints one at a time,
        # find which one restores feasibility, and report it in business terms.
        return None, status_name, runtime, None

    solution = pd.DataFrame(
        {
            "row_id": candidates["row_id"].to_numpy(),
            "label": candidates["label"].to_numpy(),
            "category": category,
            "selected": [int(round(x[i].solution_value())) for i in range(n)],
            "value": value,
            COST_COLUMN: cost,
            "mpg": mpg,
        }
    )
    return solution, status_name, runtime, float(solver.Objective().Value())


# ---------------------------------------------------------------------------
# 6. Verify - independently, against source, not the query that built candidates
# ---------------------------------------------------------------------------


def verify(wb, solution: pd.DataFrame, log: ConstraintLog) -> pd.DataFrame:
    chosen_ids = solution.loc[solution["selected"] == 1, "row_id"].tolist()

    if not chosen_ids:
        raise AssertionError("solver returned an empty selection")

    # Recompute from `source` so a bug in the candidate query cannot hide here.
    id_list = ", ".join(str(int(i)) for i in chosen_ids)
    chosen = wb.sql(
        f"""
        SELECT row_id, {LABEL_COLUMN} AS label, {CATEGORY_COLUMN} AS category,
               {VALUE_COLUMN} AS value, weight * {COST_PER_KG} AS unit_cost, mpg
        FROM source
        WHERE row_id IN ({id_list})
        """
    )

    fleet_size = len(chosen)
    log.set_achieved("budget", float(chosen["unit_cost"].sum()))
    log.set_achieved("fleet_size", float(fleet_size))
    log.set_achieved("avg_mpg", float(chosen["mpg"].mean()))

    shares = chosen["category"].value_counts(normalize=True)
    for constraint in log.items:
        if constraint.name.startswith("share_"):
            group = constraint.name.removeprefix("share_")
            log.set_achieved(constraint.name, float(shares.get(group, 0.0)))

    violations = log.violations()
    if violations:
        raise AssertionError(
            "solution violates constraints that were supposed to hold: "
            + ", ".join(f"{c.name} ({c.achieved} vs {c.sense} {c.bound})" for c in violations)
        )

    return chosen


# ---------------------------------------------------------------------------
# 7. Explain
# ---------------------------------------------------------------------------


def write_report(wb, chosen: pd.DataFrame, log: ConstraintLog, objective: float) -> str:
    constraints = log.to_frame()
    binding = constraints.loc[constraints["binding"].fillna(False).astype(bool)]

    table = chosen.assign(
        unit_cost=lambda d: d["unit_cost"].round(0),
        mpg=lambda d: d["mpg"].round(1),
    )[["label", "category", "value", "unit_cost", "mpg"]].rename(
        columns={
            "label": "Model",
            "category": "Origin",
            "value": "Horsepower",
            "unit_cost": "Cost",
            "mpg": "MPG",
        }
    )

    spend = float(chosen["unit_cost"].sum())
    lines = [
        "# Fleet procurement recommendation",
        "",
        "## Recommendation",
        "",
        f"Buy the **{len(chosen)} vehicles** listed below for a total of "
        f"**{spend:,.0f}**, delivering **{objective:,.0f} cumulative horsepower** "
        f"at a fleet average of **{chosen['mpg'].mean():.1f} mpg**.",
        "",
        "## What this achieves",
        "",
        f"- Cumulative engine power: **{objective:,.0f} hp**",
        f"- Capital deployed: **{spend:,.0f}** of {BUDGET:,.0f} "
        f"({spend / BUDGET:.0%} of budget)",
        f"- Fleet average efficiency: **{chosen['mpg'].mean():.1f} mpg** "
        f"against a {MIN_AVG_MPG:g} mpg target",
        "",
        "## The decision",
        "",
        md_table(table),
        "",
        "## Why these and not others",
        "",
    ]

    if len(binding):
        lines.append("These constraints are binding - they are what stops a better answer:")
        lines.append("")
        for row in binding.itertuples():
            lines.append(f"- **{row.business_rule}** (at {row.achieved:,.2f})")
    else:
        lines.append("No constraint is tight; the objective is limited by the catalogue itself.")

    lines += [
        "",
        "## What would change the answer",
        "",
        "The binding constraints above are the levers. Relaxing one of them is "
        "the only way to a higher-power fleet; relaxing anything else changes "
        "nothing.",
        "",
        "## Constraint check",
        "",
        md_table(
            constraints[
                ["business_rule", "sense", "bound", "achieved", "slack", "binding", "satisfied"]
            ].rename(columns={"business_rule": "Rule"})
        ),
        "",
        "## Assumptions",
        "",
    ]
    lines += [f"- {a}" for a in wb.assumptions()] or ["- None."]
    lines += [
        "",
        "## How to check this",
        "",
        "```bash",
        "riskon sql \"SELECT * FROM constraints\"",
        "riskon sql \"SELECT * FROM solution WHERE selected = 1\"",
        "```",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def main() -> int:
    wb = connect()
    log = ConstraintLog()

    candidates = build_candidates(wb)
    print(f"candidates: {len(candidates):,} rows")

    solution, status, runtime, objective = solve(candidates, log)
    print(f"status: {status} in {runtime:.3f}s")

    if solution is None:
        wb.record(constraints=log, status=status, runtime_seconds=runtime)
        print("\nINFEASIBLE. Drop constraints one at a time to find the conflict;")
        print("do not simply loosen a bound. See AGENTS.md step 5.")
        wb.close()
        return 1

    chosen = verify(wb, solution, log)
    wb.record(
        solution=solution,
        constraints=log,
        status=status,
        objective=objective,
        runtime_seconds=runtime,
        solver="ortools.pywraplp/SCIP",
        model="selection_milp",
    )

    report = write_report(wb, chosen, log, objective)
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
