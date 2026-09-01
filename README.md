# riskon

An AI-powered Operations Research (OR) agent that turns natural-language business
questions into solved optimization models — and translates the results back into
plain-language recommendations.

## The idea

Business stakeholders know the question ("what fleet should we buy with this
budget?"), but the answer requires building a mathematical optimization model.
This project closes that gap with an end-to-end pipeline:

1. Ingest a business problem written in natural language
2. Inspect the accompanying tabular dataset (headers, types, ranges)
3. Formulate an appropriate optimization model
4. Generate and run solver code (OR-Tools, Gurobi, SciPy)
5. Explain the solution as an executive-ready summary

## Contents

| File | Description |
| --- | --- |
| `HACKATHON_BRIEF.md` | The original challenge brief and dataset tracks |
| `LAGRANGE-Implementation-Plan.pdf` / `implementation-plan.html` | Implementation plan |
| `Fleet-Decision-Report.pdf` / `fleet-decision-report.html` | Example output: corporate fleet procurement decision report |

## Example track

**Corporate fleet procurement** — maximize cumulative engine power across a
vehicle catalog while staying inside a fixed capital budget and corporate
emission targets. See `Fleet-Decision-Report.pdf` for the generated report.
