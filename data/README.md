# Datasets

Committed so a demo never depends on the network. `riskon load` also takes an
http(s) URL, so a prompt can paste a link instead of naming a file here.

Nothing reads these files directly. `riskon load` converts them into the run's
`workbench.duckdb`, and every downstream step reads tables.

## `mpg.csv` - 398 rows

Vehicle catalogue: `mpg`, `cylinders`, `displacement`, `horsepower`, `weight`,
`acceleration`, `model_year`, `origin`, `name`.

**Track A - Corporate Fleet Procurement.** Rows are purchasable models. Maximise
cumulative engine power under a capital budget and a sustainability target.
Typically a binary/integer selection MILP.

Watch for: `horsepower` has nulls. There is no price column, so a cost proxy has
to be derived and recorded in the assumption ledger.

Source: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/mpg.csv

## `diamonds.csv` - 53,940 rows

Market inventory: `carat`, `cut`, `color`, `clarity`, `depth`, `table`, `price`,
`x`, `y`, `z`.

**Track B - Luxury Vault Stocking.** Deploy a fixed line of credit across a
diversified portfolio. Maximise carats or margin subject to display-case limits
and bounded risk categories, e.g. no single `cut` grade above 30% of inventory.

Watch for: too large to model whole. Sample in SQL first (`USING SAMPLE`, or a
stratified sample per `cut`), and record the sampling query - `materialize()`
stores it in `meta` automatically.

Source: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/diamonds.csv

## `taxis.csv` - 6,433 rows

Trip log: `pickup`, `dropoff`, `passengers`, `distance`, `fare`, `tip`, `tolls`,
`total`, `color`, `payment`, `pickup_zone`, `dropoff_zone`, `pickup_borough`,
`dropoff_borough`.

**Track C - Urban Dispatch Assignment.** Rows are pending tasks with a revenue
(`total`) and an occupied interval (`pickup` to `dropoff`). Assign tasks to a
driver pool to maximise revenue, with no driver holding overlapping trips and
passenger counts within vehicle capacity. Usually CP-SAT.

Watch for: `pickup`/`dropoff` are timestamps - take one operational slice
(an hour or two) or the assignment matrix explodes. The driver pool is not in
the data; its size is an assumption and belongs in the ledger.

Source: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/taxis.csv
