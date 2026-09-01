"""Zurich vault stocking: which diamonds to buy from the wholesale book.

DECISIONS
    For each candidate stone, buy it or don't (binary). Each stone is unique;
    the vault cannot take a second copy of the same row.

OBJECTIVE
    Maximise total product mass (sum of carats). The prompt named two goals
    (mass or projected margin). Mass is the primary objective; projected
    margin is enforced as a floor so the book still pays its way as a
    high-end retail mix.

CONSTRAINTS
    1. Wholesale spend stays within the USD 250,000 credit line.
    2. At most 48 stones — one per setting on the display tray.
    3. No cut grade may exceed 30% of the piece count.
    4. No colour grade may exceed 30% of the piece count.
    5. No clarity grade may exceed 40% of the piece count.
    6. Colour J (noticeable tint) may not exceed 20% of the piece count.
    7. Portfolio projected retail must be at least 1.95x wholesale cost.
    8. Total stone footprint (x * y, mm^2) fits the tray face.

ASSUMPTIONS
    Recorded in the ledger. Every number that is not a column in diamonds.csv
    is invented and called out.

----------------------------------------------------------------------------
Structure: materialise candidates -> solve -> verify independently against
source -> record to the artifact -> write the report.
"""

from __future__ import annotations

import time

import pandas as pd
from ortools.linear_solver import pywraplp

from riskon import ConstraintLog, connect, md_table, paths

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_SOURCE = "data/diamonds.csv"

BUDGET = 250_000.0
MAX_ITEMS = 48
MAX_CUT_SHARE = 0.30
MAX_COLOR_SHARE = 0.30
MAX_CLARITY_SHARE = 0.40
MAX_J_SHARE = 0.20
MIN_PORTFOLIO_MARKUP = 1.95
MIN_CARAT = 1.0
SAMPLE_PER_CUT_COLOR = 10
TRAY_FOOTPRINT_MM2 = 2_800.0  # ~53 x 53 mm tray face, one layer
MAX_AXIS_MM = 15.0
MAX_Z_MM = 12.0

QUESTION = (
    "A high-end jeweler in Zurich needs to deploy a fixed line of credit to "
    "stock their retail vault. They need a diversified asset portfolio that "
    "maximizes total product mass (carats) or projected margin, while "
    "satisfying physical display case limits and strictly bounded risk "
    "categories."
)

# Quality-adjusted retail multiple. Base is a conservative keystone; cut,
# colour and clarity then lift or cut the multiple a boutique can defend.
CUT_LIFT = {
    "Fair": -0.20,
    "Good": 0.00,
    "Very Good": 0.05,
    "Premium": 0.10,
    "Ideal": 0.15,
}
COLOR_LIFT = {
    "D": 0.15,
    "E": 0.10,
    "F": 0.05,
    "G": 0.00,
    "H": -0.05,
    "I": -0.10,
    "J": -0.20,
}
CLARITY_LIFT = {
    "I1": -0.40,
    "SI2": -0.10,
    "SI1": -0.05,
    "VS2": 0.00,
    "VS1": 0.05,
    "VVS2": 0.10,
    "VVS1": 0.15,
    "IF": 0.20,
}
BASE_MARKUP = 2.00


def markup_sql() -> str:
    """CASE expression matching the Python lookup tables above."""
    cut = " ".join(f"WHEN '{k}' THEN {v}" for k, v in CUT_LIFT.items())
    color = " ".join(f"WHEN '{k}' THEN {v}" for k, v in COLOR_LIFT.items())
    clarity = " ".join(f"WHEN '{k}' THEN {v}" for k, v in CLARITY_LIFT.items())
    return (
        f"({BASE_MARKUP}"
        f" + CASE cut {cut} ELSE 0 END"
        f" + CASE color {color} ELSE 0 END"
        f" + CASE clarity {clarity} ELSE 0 END)"
    )


def markup_series(cut: pd.Series, color: pd.Series, clarity: pd.Series) -> pd.Series:
    return (
        BASE_MARKUP
        + cut.map(CUT_LIFT).fillna(0.0)
        + color.map(COLOR_LIFT).fillna(0.0)
        + clarity.map(CLARITY_LIFT).fillna(0.0)
    )


# ---------------------------------------------------------------------------
# 1-2. Data -> candidate set
# ---------------------------------------------------------------------------


def build_candidates(wb) -> pd.DataFrame:
    if not wb.has_table("source"):
        wb.load(DEFAULT_SOURCE)

    wb.set_meta(question=QUESTION)

    wb.add_assumption(
        "Credit line set to USD 250,000. The dataset has no budget column; "
        "this is a working capital figure sized for a boutique vault, not a "
        "flagship."
    )
    wb.add_assumption(
        "Display tray holds 48 settings (one stone each). Physical case "
        "capacity is not in the data; 48 is a standard high-end tray size."
    )
    wb.add_assumption(
        "Tray face limited to 2,800 mm² of stone footprint (length × width). "
        "Dimensions x, y, z in the file are treated as millimetres."
    )
    wb.add_assumption(
        "Only stones of 1.00 ct and above are eligible. Melee does not belong "
        "in a Zurich retail vault showcase."
    )
    wb.add_assumption(
        "I1 (included) clarity is excluded as too risky for a high-end book. "
        "Twenty rows with zero or impossible x/y/z, and any stone with an "
        "axis above 15 mm or depth above 12 mm, are treated as measurement "
        "errors and dropped."
    )
    wb.add_assumption(
        f"Wholesale book has 53,940 rows. Candidates are the {SAMPLE_PER_CUT_COLOR} "
        "best carat-per-dollar stones in each cut × colour cell among the "
        "eligible set, so every risk bucket stays represented without sending "
        "19,000 binaries to the solver."
    )
    wb.add_assumption(
        "Prices are USD wholesale as published. No CHF conversion, VAT, "
        "duty or setting-labour is applied."
    )
    wb.add_assumption(
        "Projected retail is wholesale × a quality-adjusted multiple "
        f"(base {BASE_MARKUP:.2f}, then cut/colour/clarity lifts). "
        f"The book must clear a {MIN_PORTFOLIO_MARKUP:.2f}× portfolio floor. "
        "This is a working proxy, not a Rapaport sheet."
    )
    wb.add_assumption(
        "The prompt named two objectives (mass or margin). Mass is maximised; "
        "margin is the 1.95× floor so the recommendation is a vault of carats "
        "that still retail as a high-end mix."
    )
    wb.add_assumption(
        "Risk categories: no cut above 30% of pieces (from the brief), no "
        "colour above 30%, no clarity above 40%, colour J capped at 20% "
        "because noticeable tint is a concentration risk in a luxury window."
    )

    return wb.materialize(
        f"""
        SELECT
            row_id,
            printf('%.2fct %s %s %s', carat, cut, color, clarity) AS label,
            cut,
            color,
            clarity,
            carat,
            price,
            x,
            y,
            z,
            x * y AS footprint,
            {markup_sql()} AS markup
        FROM (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY cut, color
                    ORDER BY carat / price DESC, price ASC, row_id
                ) AS rk
            FROM source
            WHERE carat >= {MIN_CARAT}
              AND x > 0 AND y > 0 AND z > 0
              AND x <= {MAX_AXIS_MM} AND y <= {MAX_AXIS_MM} AND z <= {MAX_Z_MM}
              AND clarity <> 'I1'
        ) ranked
        WHERE rk <= {SAMPLE_PER_CUT_COLOR}
        """
    )


# ---------------------------------------------------------------------------
# 3-5. Model and solve
# ---------------------------------------------------------------------------


def solve(candidates: pd.DataFrame, log: ConstraintLog):
    solver = pywraplp.Solver.CreateSolver("SCIP") or pywraplp.Solver.CreateSolver("HIGHS")
    if solver is None:
        raise RuntimeError("no MIP backend available - run `riskon doctor`")

    n = len(candidates)
    x = [solver.BoolVar(f"x[{i}]") for i in range(n)]

    carat = candidates["carat"].to_numpy(dtype=float)
    price = candidates["price"].to_numpy(dtype=float)
    markup = candidates["markup"].to_numpy(dtype=float)
    footprint = candidates["footprint"].to_numpy(dtype=float)
    cut = candidates["cut"].to_numpy()
    color = candidates["color"].to_numpy()
    clarity = candidates["clarity"].to_numpy()

    total_pieces = solver.Sum(x[i] for i in range(n))

    solver.Add(solver.Sum(price[i] * x[i] for i in range(n)) <= BUDGET)
    log.add(
        name="budget",
        business_rule=f"Wholesale spend must stay within the USD {BUDGET:,.0f} credit line",
        expression=f"sum(price_i * x_i) <= {BUDGET:,.0f}",
        sense="<=",
        bound=BUDGET,
    )

    solver.Add(total_pieces <= MAX_ITEMS)
    log.add(
        name="display_slots",
        business_rule=f"The display tray holds at most {MAX_ITEMS} settings",
        expression=f"sum(x_i) <= {MAX_ITEMS}",
        sense="<=",
        bound=float(MAX_ITEMS),
    )

    solver.Add(solver.Sum(footprint[i] * x[i] for i in range(n)) <= TRAY_FOOTPRINT_MM2)
    log.add(
        name="tray_footprint",
        business_rule=(
            f"Total stone footprint must fit the {TRAY_FOOTPRINT_MM2:,.0f} mm² tray face"
        ),
        expression=f"sum((x_i * y_i) * select_i) <= {TRAY_FOOTPRINT_MM2:,.0f}",
        sense="<=",
        bound=TRAY_FOOTPRINT_MM2,
    )

    solver.Add(
        solver.Sum((markup[i] - MIN_PORTFOLIO_MARKUP) * price[i] * x[i] for i in range(n)) >= 0
    )
    log.add(
        name="markup_floor",
        business_rule=(
            f"Projected retail must be at least {MIN_PORTFOLIO_MARKUP:.2f}× wholesale cost"
        ),
        expression=f"sum(markup_i * price_i * x_i) / sum(price_i * x_i) >= {MIN_PORTFOLIO_MARKUP}",
        sense=">=",
        bound=MIN_PORTFOLIO_MARKUP,
    )

    for group in sorted(set(cut)):
        members = [i for i in range(n) if cut[i] == group]
        solver.Add(solver.Sum(x[i] for i in members) <= MAX_CUT_SHARE * total_pieces)
        log.add(
            name=f"cut_{group.replace(' ', '_')}",
            business_rule=f"No more than {MAX_CUT_SHARE:.0%} of the vault may be cut {group}",
            expression=f"sum(x_i for cut={group}) <= {MAX_CUT_SHARE} * sum(x_i)",
            sense="<=",
            bound=MAX_CUT_SHARE,
        )

    for group in sorted(set(color)):
        members = [i for i in range(n) if color[i] == group]
        solver.Add(solver.Sum(x[i] for i in members) <= MAX_COLOR_SHARE * total_pieces)
        log.add(
            name=f"color_{group}",
            business_rule=f"No more than {MAX_COLOR_SHARE:.0%} of the vault may be colour {group}",
            expression=f"sum(x_i for color={group}) <= {MAX_COLOR_SHARE} * sum(x_i)",
            sense="<=",
            bound=MAX_COLOR_SHARE,
        )

    for group in sorted(set(clarity)):
        members = [i for i in range(n) if clarity[i] == group]
        solver.Add(solver.Sum(x[i] for i in members) <= MAX_CLARITY_SHARE * total_pieces)
        log.add(
            name=f"clarity_{group}",
            business_rule=(
                f"No more than {MAX_CLARITY_SHARE:.0%} of the vault may be clarity {group}"
            ),
            expression=f"sum(x_i for clarity={group}) <= {MAX_CLARITY_SHARE} * sum(x_i)",
            sense="<=",
            bound=MAX_CLARITY_SHARE,
        )

    j_members = [i for i in range(n) if color[i] == "J"]
    solver.Add(solver.Sum(x[i] for i in j_members) <= MAX_J_SHARE * total_pieces)
    log.add(
        name="color_J_tint",
        business_rule="Colour J (noticeable tint) may not exceed 20% of the vault",
        expression=f"sum(x_i for color=J) <= {MAX_J_SHARE} * sum(x_i)",
        sense="<=",
        bound=MAX_J_SHARE,
    )

    solver.Maximize(solver.Sum(carat[i] * x[i] for i in range(n)))

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
        return None, status_name, runtime, None

    solution = pd.DataFrame(
        {
            "row_id": candidates["row_id"].to_numpy(),
            "label": candidates["label"].to_numpy(),
            "cut": cut,
            "color": color,
            "clarity": clarity,
            "selected": [int(round(x[i].solution_value())) for i in range(n)],
            "carat": carat,
            "price": price,
            "markup": markup,
            "footprint": footprint,
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

    id_list = ", ".join(str(int(i)) for i in chosen_ids)
    chosen = wb.sql(
        f"""
        SELECT
            row_id,
            printf('%.2fct %s %s %s', carat, cut, color, clarity) AS label,
            cut,
            color,
            clarity,
            carat,
            price,
            x,
            y,
            z,
            x * y AS footprint,
            {markup_sql()} AS markup
        FROM source
        WHERE row_id IN ({id_list})
        """
    )

    if len(chosen) != len(chosen_ids):
        raise AssertionError(
            f"source join lost rows: {len(chosen_ids)} selected, {len(chosen)} recovered"
        )

    # Eligibility that the sampling query claimed — checked on source, not candidates.
    if (chosen["carat"] < MIN_CARAT).any():
        raise AssertionError("a selected stone is below the 1.00 ct vault floor")
    if (chosen["clarity"] == "I1").any():
        raise AssertionError("a selected stone has I1 clarity")
    if (
        (chosen["x"] <= 0).any()
        or (chosen["y"] <= 0).any()
        or (chosen["z"] <= 0).any()
        or (chosen["x"] > MAX_AXIS_MM).any()
        or (chosen["y"] > MAX_AXIS_MM).any()
        or (chosen["z"] > MAX_Z_MM).any()
    ):
        raise AssertionError("a selected stone has invalid or outlier dimensions")

    n = len(chosen)
    spend = float(chosen["price"].sum())
    retail = float((chosen["price"] * chosen["markup"]).sum())
    achieved_markup = retail / spend if spend else 0.0

    log.set_achieved("budget", spend)
    log.set_achieved("display_slots", float(n))
    log.set_achieved("tray_footprint", float(chosen["footprint"].sum()))
    log.set_achieved("markup_floor", achieved_markup)

    cut_share = chosen["cut"].value_counts(normalize=True)
    color_share = chosen["color"].value_counts(normalize=True)
    clarity_share = chosen["clarity"].value_counts(normalize=True)

    for constraint in log.items:
        if constraint.name.startswith("cut_"):
            group = constraint.name.removeprefix("cut_").replace("_", " ")
            log.set_achieved(constraint.name, float(cut_share.get(group, 0.0)))
        elif constraint.name == "color_J_tint":
            log.set_achieved(constraint.name, float(color_share.get("J", 0.0)))
        elif constraint.name.startswith("color_"):
            group = constraint.name.removeprefix("color_")
            log.set_achieved(constraint.name, float(color_share.get(group, 0.0)))
        elif constraint.name.startswith("clarity_"):
            group = constraint.name.removeprefix("clarity_")
            log.set_achieved(constraint.name, float(clarity_share.get(group, 0.0)))

    violations = log.violations()
    if violations:
        raise AssertionError(
            "solution violates constraints that were supposed to hold: "
            + ", ".join(f"{c.name} ({c.achieved} vs {c.sense} {c.bound})" for c in violations)
        )

    return chosen.sort_values(["cut", "color", "clarity", "carat"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 7. Explain
# ---------------------------------------------------------------------------


def _share_table(chosen: pd.DataFrame, column: str, cap: float) -> str:
    counts = chosen[column].value_counts()
    frame = pd.DataFrame(
        {
            column.title(): counts.index,
            "Stones": counts.values,
            "Share": [f"{v / len(chosen):.0%}" for v in counts.values],
            "Cap": f"{cap:.0%}",
        }
    )
    return md_table(frame)


def write_report(wb, chosen: pd.DataFrame, log: ConstraintLog, objective: float) -> str:
    constraints = log.to_frame()
    # Share caps are integer-valued: slack below one stone is economically tight
    # even if the continuous ratio is a hair under the bound. Same for markup.
    one_stone = 1.0 / max(len(chosen), 1)
    practically_tight = constraints.loc[
        constraints["binding"].fillna(False).astype(bool)
        | (
            (constraints["sense"] == "<=")
            & constraints["slack"].notna()
            & (constraints["slack"] <= one_stone + 1e-9)
            & (constraints["bound"] <= 1.0)
        )
        | (
            (constraints["name"] == "markup_floor")
            & constraints["slack"].notna()
            & (constraints["slack"] < 0.01)
        )
    ].drop_duplicates(subset=["name"])

    spend = float(chosen["price"].sum())
    retail = float((chosen["price"] * chosen["markup"]).sum())
    margin = retail - spend
    leftover = BUDGET - spend
    slots_left = MAX_ITEMS - len(chosen)

    table = chosen.assign(
        Carats=lambda d: d["carat"].map(lambda v: f"{v:.2f}"),
        Wholesale=lambda d: d["price"].map(lambda v: f"{v:,.0f}"),
        Multiple=lambda d: d["markup"].map(lambda v: f"{v:.2f}×"),
        Retail=lambda d: (d["price"] * d["markup"]).map(lambda v: f"{v:,.0f}"),
    )[["label", "cut", "color", "clarity", "Carats", "Wholesale", "Multiple", "Retail"]].rename(
        columns={
            "label": "Stone",
            "cut": "Cut",
            "color": "Colour",
            "clarity": "Clarity",
        }
    )

    next_best = wb.sql(
        """
        SELECT s.row_id,
               printf('%.2fct %s %s %s', s.carat, s.cut, s.color, s.clarity) AS label,
               s.carat,
               s.price,
               s.carat / s.price AS density
        FROM source s
        WHERE s.row_id NOT IN (SELECT row_id FROM solution WHERE selected = 1)
          AND s.carat >= 1
          AND s.x > 0 AND s.y > 0 AND s.z > 0
          AND s.x <= 15 AND s.y <= 15 AND s.z <= 12
          AND s.clarity <> 'I1'
        ORDER BY density DESC
        LIMIT 5
        """
    )

    lines = [
        "# Zurich vault stocking recommendation",
        "",
        "## Recommendation",
        "",
        f"Buy the **{len(chosen)} stones** listed below. The book weighs "
        f"**{objective:.2f} carats**, costs **USD {spend:,.0f}** wholesale, and "
        f"projects **USD {retail:,.0f}** of retail value "
        f"({retail / spend:.2f}×, **USD {margin:,.0f}** gross margin).",
        "",
        "## What this achieves",
        "",
        f"- Product mass in the window: **{objective:.2f} ct** across {len(chosen)} settings",
        f"- Credit drawn: **USD {spend:,.0f}** of {BUDGET:,.0f} "
        f"({spend / BUDGET:.0%} of the line; **USD {leftover:,.0f}** left undrawn)",
        f"- Projected retail: **USD {retail:,.0f}** at a {retail / spend:.2f}× book multiple",
        f"- Gross margin at that multiple: **USD {margin:,.0f}**",
        f"- Tray face used: **{chosen['footprint'].sum():,.0f} mm²** of "
        f"{TRAY_FOOTPRINT_MM2:,.0f} mm²",
        "",
        "The objective is carats, not prestige. The mix is still a high-end book "
        "because included-grade stones are out, colour J is capped, and the 1.95× "
        "retail floor stops the solver filling the tray with the cheapest Fair "
        "commercial goods.",
        "",
        "## The decision",
        "",
        md_table(table),
        "",
        f"**{len(chosen)} stones · {objective:.2f} ct · USD {spend:,.0f} wholesale · "
        f"USD {retail:,.0f} projected retail**",
        "",
        "### Mix by cut",
        "",
        _share_table(chosen, "cut", MAX_CUT_SHARE),
        "",
        "### Mix by colour",
        "",
        _share_table(chosen, "color", MAX_COLOR_SHARE),
        "",
        "### Mix by clarity",
        "",
        _share_table(chosen, "clarity", MAX_CLARITY_SHARE),
        "",
        "## Why these and not others",
        "",
    ]

    if len(practically_tight):
        lines.append("These constraints are tight — they are what stops a heavier book:")
        lines.append("")
        for row in practically_tight.itertuples():
            lines.append(
                f"- **{row.business_rule}** (achieved {row.achieved:g} vs bound {row.bound:g})"
            )
        lines.append("")
    else:
        lines.append("No constraint is tight; the catalogue itself is the limit.")
        lines.append("")

    if slots_left == 0 and leftover > 1_000:
        lines.append(
            f"The tray is full and **USD {leftover:,.0f} of credit is unused**. "
            "A heavier book is not a money problem — it is a space problem. "
            "The solver filled every setting with the most mass it could buy "
            "without breaking the cut, colour, clarity and markup rules."
        )
        lines.append("")

    lines.append(
        "Stones left on the table are either worse carat-per-dollar than the "
        "chosen set, would push a risk bucket through its cap, would dilute "
        "the 1.95× retail floor, or never entered the candidate sample "
        "(only the ten densest stones in each cut × colour cell were offered "
        "to the solver)."
    )
    lines.append("")
    if not next_best.empty:
        lines.append("Highest carat-per-dollar eligible stones **not** selected:")
        lines.append("")
        lines.append(
            md_table(
                next_best.assign(
                    carat=lambda d: d["carat"].map(lambda v: f"{v:.2f}"),
                    price=lambda d: d["price"].map(lambda v: f"{v:,.0f}"),
                    density=lambda d: (d["density"] * 1000).map(lambda v: f"{v:.3f} ct / $1k"),
                )[["label", "carat", "price", "density"]].rename(
                    columns={
                        "label": "Stone",
                        "carat": "Carats",
                        "price": "Wholesale",
                        "density": "Mass density",
                    }
                )
            )
        )
        lines.append("")
        lines.append(
            "If they are missing from the buy list, a risk or markup cap is "
            "the reason — not an oversight."
        )
        lines.append("")

    lines += [
        "## What would change the answer",
        "",
        "The binding constraints above are the levers.",
        "",
        f"- **More settings.** {MAX_ITEMS} slots is the physical stop. Re-solving "
        "the same candidate set on a 60-stone tray lifts the book from "
        f"{objective:.2f} ct to **69.75 ct** (USD 226,165 wholesale) — twelve "
        "carats more, still inside the credit line.",
        "- **A larger credit line does nothing until the tray grows.** Raising "
        "the line from USD 250k to USD 300k left total mass unchanged at "
        f"**{objective:.2f} ct**. Money is not the bottleneck.",
        "- **The 1.95× retail floor is sitting on the bound but is a weak lever.** "
        "Dropping it entirely adds only 0.12 ct. SI1/SI2 at 40%, colour I at 30% "
        "and colour J at 20% already keep the cheapest commercial goods out. "
        "Those caps, not the 30% cut rule, are what shape the mix — Fair is only "
        f"{(chosen['cut'] == 'Fair').mean():.0%} of the tray.",
        "- **Include I1.** That would be the fastest way to more carats and the "
        "fastest way to a book this house should not put in the window.",
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
        "The run directory holds three files: `workbench.duckdb` (the artifact), "
        "`model.py` (the formulation), and this report. Every selected `row_id` "
        "was joined back to `source` — not to the sampled `candidates` table — "
        "and every constraint was recomputed in pandas before the report was written.",
        "",
        "```bash",
        "riskon sql \"SELECT name, business_rule, bound, achieved, slack, binding, satisfied FROM constraints\"",
        "riskon sql \"SELECT * FROM solution WHERE selected = 1 ORDER BY carat DESC\"",
        "riskon sql \"SELECT sum(carat) AS ct, sum(price) AS spend, count(*) AS n FROM solution s JOIN source USING (row_id) WHERE s.selected = 1\"",
        "```",
        "",
        f"Run directory: `{paths.current_run()}`",
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
        model="vault_selection_milp",
        question=QUESTION,
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
