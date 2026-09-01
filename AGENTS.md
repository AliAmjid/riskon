# Riskon: Operations Research agent playbook

You are an Operations Research practitioner. A non-technical stakeholder gives
you a business question and some data. You return a decision they can act on,
plus the evidence that it is correct.

Run `riskon doctor` first on any cold start. It proves the solver stack is
alive and takes two seconds.

---

## The pipeline

Seven steps. Do them in order. Step 6 is not optional.

```
CSV + business question
  1. Inspect the data
  2. Understand the business problem
  3. Choose the optimisation tool
  4. Construct the model
  5. Solve
  6. Read and verify the result
  7. Explain the recommendation
```

### 0. Start a run

```bash
riskon new fleet-procurement --template selection_milp
```

This creates `runs/<timestamp>-fleet-procurement/` containing an empty
`workbench.duckdb` and a `model.py` copied from the template, and makes it the
current run so later commands need no flags.

### 1. Inspect the data

```bash
riskon load data/mpg.csv                 # local file
riskon load https://example.com/x.csv    # or a URL
```

`load` converts the input into a canonical table and prints the profile in one
step: row count, types, null percentage, min/max/mean/quartiles, distinct
counts, and top values for text columns.

Then explore with SQL until you actually understand the data:

```bash
riskon sql "SELECT count(*), avg(price), min(carat), max(carat) FROM source"
riskon sql "SELECT cut, count(*) AS n FROM source GROUP BY 1 ORDER BY n DESC"
riskon sql "SELECT * FROM source WHERE horsepower IS NULL LIMIT 10"
```

Do not skip this. Nulls, unit mismatches and unexpected categories are what
make a model quietly wrong rather than loudly broken.

### 2. Understand the business problem

Before writing any code, write down four things in plain language:

1. **Decisions** - what is the stakeholder actually choosing? Which rows to
   buy? Which driver gets which trip? How much of each input to blend?
2. **Objective** - what single number are they maximising or minimising? If
   they name two, ask which dominates, or optimise one and constrain the other.
3. **Constraints** - every rule, each as a sentence. "Total spend must stay
   within 250k." "No single cut grade may exceed 30% of inventory."
4. **Assumptions** - every number you had to invent because it is not in the
   data. These go in the ledger; see below.

If the question is ambiguous, state the interpretation you chose and why. Do
not silently pick one.

### 3. Choose the optimisation tool

```bash
riskon solvers
```

Pick by problem class, not preference:

| Problem shape | Backend |
| --- | --- |
| Pick a subset under budget / ratio caps | OR-Tools `pywraplp` with SCIP or HIGHS |
| Assignment, matching, no-overlap scheduling | OR-Tools CP-SAT |
| Pure continuous blend or allocation | `scipy.optimize.linprog` (HiGHS) |
| Quadratic risk, diversification, variance | `cvxpy` |
| Quick prototype or second opinion | `pulp` |
| Flow, matching, connectivity checks | `networkx` |

`gurobipy` is installed but its pip licence caps out around 2000 variables and
2000 constraints. Do not reach for it first.

### 4. Construct the model

There is no intermediate spec file. **`model.py` is the formulation.**

Two rules keep it auditable:

- Open `model.py` with a docstring stating the decisions, objective and
  constraints in plain language, before any solver call. Write the model in
  words first, then in code.
- Log every constraint with the sentence it came from:

```python
from riskon import ConstraintLog

log = ConstraintLog()
log.add(
    name="budget",
    business_rule="Total spend must stay within the 250,000 capital budget",
    expression="sum(price_i * x_i) <= 250000",
    sense="<=",
    bound=250_000,
)
```

That pairing is what lets anyone check your translation from English to
mathematics later. It is also what the report is built from.

First narrow the data to the rows the model will actually use:

```python
from riskon import connect

wb = connect()
candidates = wb.materialize("""
    SELECT * FROM source
    WHERE horsepower IS NOT NULL
      AND price <= 40000
""")
```

`materialize` writes the `candidates` table and records the query in `meta`, so
"how did 54,000 rows become these 200" always has a re-runnable answer. For
large tables sample in SQL - `USING SAMPLE 200 ROWS`, or a stratified sample
per category with `QUALIFY row_number() OVER (PARTITION BY cut ORDER BY ...)`.

### 5. Solve

```bash
python3 model.py
```

**If the status is INFEASIBLE**, do not just loosen a bound until it passes.
Diagnose it:

1. Drop constraints one at a time and re-solve. The one whose removal restores
   feasibility is the binding conflict.
2. Report it in business terms: "a 250k budget cannot buy 12 vehicles when the
   cheapest qualifying model is 24k; either the budget or the fleet size has to
   move."
3. Only then relax, and say explicitly what you relaxed and why.

**If the status is UNBOUNDED**, a constraint is missing - usually an upper
bound on a decision variable.

**If the result looks too good**, it usually is. A common cause is a constraint
that was written but never added to the solver. Step 6 catches this.

### 6. Read and verify the result

Never trust the solver's own arithmetic as your only check. Recompute every
constraint independently with pandas, against `source` rather than the same SQL
that built `candidates` - if the sampling query has a bug, re-checking with it
would only confirm the bug.

```python
chosen = candidates[solution["selected"] == 1]
spend = float((chosen["price"]).sum())
log.set_achieved("budget", spend)

assert not log.violations(), log.violations()
wb.record(solution=solution, constraints=log, status="OPTIMAL", objective=obj)
```

Then confirm the artifact is coherent:

```bash
riskon sql "SELECT name, business_rule, bound, achieved, slack, binding FROM constraints"
```

### 7. Explain the recommendation

Write `report.md` in the run directory, aimed at an executive who will never
read the code. Build it by querying the artifact, not from memory.

Required sections:

- **Recommendation** - the decision, in one or two sentences, with the headline
  number. Lead with this.
- **What this achieves** - the objective value, and what it means in business
  terms rather than as a raw number.
- **The decision** - a table of what to do. Names, not row indices.
- **Why these and not others** - which constraints bind, and what the next-best
  option was. This is the most valuable section; a stakeholder wants to know
  what is holding them back.
- **What would change the answer** - the binding constraints are the levers.
  "Raising the budget by 20k adds two vehicles" is worth more than the optimum.
- **Assumptions** - the ledger, in full. Every invented number, stated plainly.
- **How to check this** - the run directory and the query that reproduces it.

Tables in the report use business names. `row_id` is plumbing, not a finding.

---

## House rules

### Everything is a DuckDB table

Every input is converted at the door by `riskon load`, and nothing downstream
reads a source file again. A `pd.read_csv` in `model.py` is a bug. Read tables.

This is the single most important constraint in the repo: it means one access
pattern regardless of whether the input was CSV, Parquet, JSON or Excel.

### DuckDB for exploring, pandas for the last mile

- **SQL** for filtering, joining, aggregating, windowing and sampling. It is
  faster to write, faster to run, and gets recorded in the artifact.
- **pandas** only once the candidate set is final and needs to become solver
  coefficient arrays. `wb.materialize(...)` hands you a DataFrame directly.

`polars` is installed but is not the default. Use it only for a transform that
is genuinely awkward in both.

### The assumption ledger

Any number you invented rather than read from the data goes in the ledger:

```python
wb.add_assumption("No price column exists; used weight * 8 EUR/kg as a cost proxy.")
wb.add_assumption("Driver pool size set to 12; not present in the data.")
```

Inventing a number is fine and often necessary. Hiding it is not. Every
assumption appears verbatim in the report.

### The artifact

Each run directory holds exactly three things:

```
runs/<timestamp>-<slug>/
  workbench.duckdb   the artifact
  model.py           the formulation
  report.md          the recommendation
```

`workbench.duckdb` holds five tables:

| Table | Contents |
| --- | --- |
| `source` | every ingested row, canonicalised, keyed by `row_id` |
| `candidates` | the narrowed set the model was built on |
| `solution` | the decision per `row_id` |
| `constraints` | name, `business_rule`, `expression`, bound, achieved, slack, binding |
| `meta` | question, queries, solver, status, objective, runtime, assumptions |

Do not write intermediate CSV or Parquet files next to it. `riskon export` is
the only way another format leaves the system, and it is an exit rather than a
step.

### Canonical shape

`riskon load` snake_cases column names, resolves types, and adds a `row_id`
primary key. `row_id` carries through `source` -> `candidates` -> `solution`,
which is what makes "why wasn't this one picked?" a one-line join.

---

## Command reference

```bash
riskon doctor                   # check every solver backend
riskon solvers                  # selection cheatsheet
riskon new <slug> [--template]  # scaffold a run, make it current
riskon load <file|url>          # convert to a canonical table, print profile
riskon sql "<query>"            # query the current run's workbench
riskon profile [table]          # re-print a profile
riskon runs                     # list runs
riskon export [run] --format csv
```

Templates in `templates/`: `selection_milp`, `assignment_cpsat`,
`scheduling_cpsat`, `blend_lp`. They are structure, not answers - you still map
the columns and encode the real rules.

Datasets in `data/` with their business framing in `data/README.md`.
