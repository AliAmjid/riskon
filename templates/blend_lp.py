"""Portfolio allocation: how much of each asset to hold.

DECISIONS
    A continuous quantity per candidate asset - no integrality anywhere, which
    is exactly why this is an LP and not a MILP.

OBJECTIVE
    Maximise total carats acquired for the credit line.

CONSTRAINTS
    1. Total outlay must stay within the credit line.
    2. No single cut grade may exceed MAX_CATEGORY_SHARE of total spend.
    3. Per-asset holdings are capped at MAX_UNITS_PER_ASSET.

----------------------------------------------------------------------------
This is a TEMPLATE. It runs as-is against data/diamonds.csv. Uses HiGHS through
scipy.optimize.linprog, which is the fastest path when nothing is integral.
If the real problem needs whole units, switch to selection_milp instead.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from riskon import ConstraintLog, connect, md_table, paths

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = "data/diamonds.csv"

SAMPLE_SIZE = 240              # diamonds.csv is 54k rows; model a sample
VALUE_COLUMN = "carat"
COST_COLUMN = "price"
CATEGORY_COLUMN = "cut"

CREDIT_LINE = 150_000.0
MAX_CATEGORY_SHARE = 0.30
MAX_UNITS_PER_ASSET = 5.0


# ---------------------------------------------------------------------------
# 1-2. Data -> candidate set
# ---------------------------------------------------------------------------


def build_candidates(wb) -> pd.DataFrame:
    if not wb.has_table("source"):
        wb.load(DEFAULT_SOURCE)

    wb.add_assumption(
        f"Modelled a stratified sample of {SAMPLE_SIZE} stones rather than all "
        f"53,940; the full registry is a market listing, not a purchase menu."
    )
    wb.add_assumption(
        "Holdings are continuous, so a fractional quantity means 'buy roughly "
        "this much of this grade', not a fraction of one stone."
    )

    # Stratified: an even spread across cut grades, so the diversification
    # constraint has something to actually bind against.
    per_group = max(1, SAMPLE_SIZE // 5)
    return wb.materialize(
        f"""
        WITH ranked AS (
            SELECT row_id, {VALUE_COLUMN} AS value, {COST_COLUMN} AS cost,
                   {CATEGORY_COLUMN} AS category, color, clarity,
                   row_number() OVER (
                       PARTITION BY {CATEGORY_COLUMN} ORDER BY {COST_COLUMN}
                   ) AS rn
            FROM source
            WHERE {COST_COLUMN} > 0 AND {VALUE_COLUMN} > 0
        )
        SELECT row_id, value, cost, category, color, clarity
        FROM ranked
        WHERE rn <= {per_group}
        ORDER BY row_id
        """
    )


# ---------------------------------------------------------------------------
# 3-5. Model and solve
# ---------------------------------------------------------------------------


def solve(candidates: pd.DataFrame, log: ConstraintLog):
    n = len(candidates)
    value = candidates["value"].to_numpy(dtype=float)
    cost = candidates["cost"].to_numpy(dtype=float)
    category = candidates["category"].to_numpy()

    # linprog minimises, so negate to maximise carats.
    c = -value

    rows: list[np.ndarray] = []
    rhs: list[float] = []

    # 1. Credit line.
    rows.append(cost.copy())
    rhs.append(CREDIT_LINE)
    log.add(
        name="credit_line",
        business_rule=f"Total outlay must stay within the {CREDIT_LINE:,.0f} credit line",
        expression=f"sum(price_i * q_i) <= {CREDIT_LINE:,.0f}",
        sense="<=",
        bound=CREDIT_LINE,
    )

    # 2. Diversification by cut grade, as a share of spend.
    for group in sorted(set(category)):
        mask = (category == group).astype(float)
        rows.append(cost * mask - MAX_CATEGORY_SHARE * cost)
        rhs.append(0.0)
        log.add(
            name=f"share_{group}",
            business_rule=(
                f"No more than {MAX_CATEGORY_SHARE:.0%} of spend may sit in "
                f"{group} cut stones"
            ),
            expression=f"sum(price_i * q_i for i in {group}) <= "
            f"{MAX_CATEGORY_SHARE} * sum(price_i * q_i)",
            sense="<=",
            bound=MAX_CATEGORY_SHARE,
        )

    started = time.perf_counter()
    result = linprog(
        c=c,
        A_ub=np.vstack(rows),
        b_ub=np.array(rhs),
        bounds=[(0.0, MAX_UNITS_PER_ASSET)] * n,
        method="highs",
    )
    runtime = time.perf_counter() - started

    if not result.success:
        return None, str(result.message), runtime, None

    quantity = np.asarray(result.x, dtype=float)
    solution = pd.DataFrame(
        {
            "row_id": candidates["row_id"].to_numpy(),
            "category": category,
            "color": candidates["color"].to_numpy(),
            "clarity": candidates["clarity"].to_numpy(),
            "quantity": quantity,
            "selected": (quantity > 1e-6).astype(int),
            "value": value,
            "cost": cost,
        }
    )
    return solution, "OPTIMAL", runtime, float(value @ quantity)


# ---------------------------------------------------------------------------
# 6. Verify independently
# ---------------------------------------------------------------------------


def verify(solution: pd.DataFrame, log: ConstraintLog) -> pd.DataFrame:
    held = solution[solution["quantity"] > 1e-6].copy()
    if held.empty:
        raise AssertionError("solver allocated nothing")

    held["spend"] = held["quantity"] * held["cost"]
    held["carats"] = held["quantity"] * held["value"]
    total_spend = float(held["spend"].sum())
    log.set_achieved("credit_line", total_spend)

    shares = held.groupby("category")["spend"].sum() / total_spend
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
    return held


# ---------------------------------------------------------------------------


def main() -> int:
    wb = connect()
    log = ConstraintLog()

    candidates = build_candidates(wb)
    print(f"candidates: {len(candidates):,} assets")

    solution, status, runtime, objective = solve(candidates, log)
    print(f"status: {status} in {runtime:.3f}s")

    if solution is None:
        wb.record(constraints=log, status=status, runtime_seconds=runtime)
        print("\nLP did not solve. Check the bounds and the constraint signs.")
        wb.close()
        return 1

    held = verify(solution, log)
    wb.record(
        solution=solution,
        constraints=log,
        status=status,
        objective=objective,
        runtime_seconds=runtime,
        solver="scipy.linprog/highs",
        model="blend_lp",
    )

    by_grade = (
        held.groupby("category")
        .agg(stones=("row_id", "count"), units=("quantity", "sum"),
             carats=("carats", "sum"), spend=("spend", "sum"))
        .round(2)
        .reset_index()
        .rename(columns={
            "category": "Cut", "stones": "Distinct stones", "units": "Units",
            "carats": "Carats", "spend": "Spend",
        })
    )

    spend = float(held["spend"].sum())
    report = "\n".join(
        [
            "# Vault stocking recommendation",
            "",
            "## Recommendation",
            "",
            f"Deploy **{spend:,.0f}** of the {CREDIT_LINE:,.0f} credit line across "
            f"**{len(held)} stones** to acquire **{objective:,.2f} carats**.",
            "",
            "## What this achieves",
            "",
            f"- Total mass acquired: **{objective:,.2f} carats**",
            f"- Capital deployed: **{spend:,.0f}** ({spend / CREDIT_LINE:.0%} of the line)",
            f"- Effective cost: **{spend / objective:,.0f} per carat**",
            "",
            "## The decision",
            "",
            md_table(by_grade),
            "",
            "## Why this mix",
            "",
            f"The {MAX_CATEGORY_SHARE:.0%} cap per cut grade is what forces "
            "diversification; without it the optimiser would concentrate "
            "entirely in whichever grade offers the best carats-per-currency.",
            "",
            "## Constraint check",
            "",
            md_table(
                log.to_frame()[
                    ["business_rule", "sense", "bound", "achieved", "slack", "binding"]
                ].rename(columns={"business_rule": "Rule"})
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
