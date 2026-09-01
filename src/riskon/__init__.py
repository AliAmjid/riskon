"""Riskon: an Operations Research workstation for an AI agent.

The workflow is fixed and lives in AGENTS.md. The only thing this package
provides is the workbench everything else is built on::

    from riskon import connect, ConstraintLog

    with connect() as wb:
        wb.load("data/mpg.csv")               # any format -> canonical table
        cand = wb.materialize("SELECT * FROM source WHERE horsepower IS NOT NULL")
        ...                                    # build and solve the model
        wb.record(solution=frame, constraints=log, status="OPTIMAL")
"""

from . import paths
from .db import (
    Constraint,
    ConstraintLog,
    Workbench,
    connect,
    md_table,
    open_run,
    workbench_path,
)
from .solvers import CHEATSHEET, doctor

__all__ = [
    "CHEATSHEET",
    "Constraint",
    "ConstraintLog",
    "Workbench",
    "connect",
    "doctor",
    "md_table",
    "open_run",
    "paths",
    "workbench_path",
]

__version__ = "0.1.0"
