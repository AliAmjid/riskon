# riskon-agent

An Operations Research workstation for an AI agent. Give it a business question
and a dataset; it returns a decision, the evidence that the decision is correct,
and an explanation a non-technical stakeholder can act on.

**The agent's instructions live in [AGENTS.md](AGENTS.md).** This file is
orientation for humans. The problem statement this was built against is in
[docs/BRIEF.md](docs/BRIEF.md).

## The pipeline

```
CSV + business question
  1. Inspect the data          riskon load <file|url>
  2. Understand the problem    decisions / objective / constraints, in words
  3. Choose the tool           riskon solvers
  4. Construct the model       runs/<id>/model.py
  5. Solve                     python3 model.py
  6. Read and verify           independent pandas re-check vs source
  7. Explain                   runs/<id>/report.md
```

## Quick start

```bash
riskon doctor                                   # is the stack alive?
riskon new fleet --template selection_milp      # scaffold a run
riskon load data/mpg.csv                        # any format -> canonical table
riskon sql "SELECT origin, count(*) FROM source GROUP BY 1"
python3 runs/*fleet/model.py                    # solve, verify, report
```

## One canonical format

Every input - CSV, TSV, Parquet, JSON, Excel, a URL, or an attached SQL
database - is converted into a DuckDB table by `riskon load`, and **nothing
downstream reads a source file again**. Model scripts read tables. A
`pd.read_csv` in a model script is a bug.

Conversion normalises shape, not just container: column names are snake_cased,
types are resolved, and a synthetic `row_id` primary key is added. `row_id`
carries through `source` -> `candidates` -> `solution`, which is what makes
"why wasn't this one picked?" a one-line join months later.

## The artifact

Each run directory holds exactly three things:

```
runs/<timestamp>-<slug>/
  workbench.duckdb   the artifact
  model.py           the formulation
  report.md          the recommendation
```

`workbench.duckdb` is a single portable file containing five tables:

| Table | Contents |
| --- | --- |
| `source` | every ingested row, canonicalised, keyed by `row_id` |
| `candidates` | the narrowed set the model was built on |
| `solution` | the decision per `row_id` |
| `constraints` | name, `business_rule`, `expression`, bound, achieved, slack, binding |
| `meta` | question, queries, solver, status, objective, runtime, assumptions |

Because it is one plain file, a finished run can be copied anywhere and
re-queried, or several can be `ATTACH`ed side by side to compare scenarios.

The `constraints` table is the interesting one: every mathematical constraint
carries the plain-language business rule it encodes, so the translation from
English to mathematics is auditable without reading the model code.

## What's installed

Solvers: OR-Tools (`pywraplp` with SCIP/CBC/GLOP/HiGHS, plus CP-SAT), PuLP,
SciPy (`linprog`/`milp`, HiGHS built in), cvxpy, Pyomo (with standalone `cbc`
and `glpsol`), gurobipy (size-limited pip licence), networkx.

Data: DuckDB, pandas, PyArrow, Polars, openpyxl.

Run `riskon doctor` to confirm, and `riskon solvers` for which one to reach for.

> **Do not install `highspy`.** It arrives as a cvxpy dependency and its
> `libhighs` symbols clash with the HiGHS bundled inside `libortools.so`,
> which breaks every OR-Tools import. The image uninstalls it explicitly, and
> `riskon doctor` checks for it. Install this package with `--no-deps`.

## Layout

```
AGENTS.md            the playbook the agent follows
src/riskon/          workbench + CLI (db.py, cli.py, solvers.py, paths.py)
templates/           model skeletons: selection_milp, vault_selection_milp,
                     assignment_cpsat, scheduling_cpsat, blend_lp
data/                the three track datasets, framed in data/README.md
tests/               end-to-end smoke test of the database-to-solver path
.cursor/             Dockerfile + environment.json for the agent image
```

## Development

```bash
docker build -f .cursor/Dockerfile -t riskon-agent .
docker run --rm -it -v "$PWD":/workspace -w /workspace riskon-agent bash
python3 -m pip install --break-system-packages --no-deps -e .
pytest
```
