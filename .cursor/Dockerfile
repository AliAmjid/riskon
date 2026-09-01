FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Toolchain the agent needs at runtime: python for the solvers, curl/jq for
# pulling CSVs, git for normal repo work. coinor-cbc and glpk-utils give Pyomo
# standalone solver binaries to shell out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        build-essential \
        pkg-config \
        git \
        curl \
        ca-certificates \
        jq \
        unzip \
        less \
        coinor-cbc \
        glpk-utils \
    && rm -rf /var/lib/apt/lists/*

# Solver stack is baked into the image so the agent never waits on a cold
# install. Installed into the system interpreter (not a venv) so that plain
# `python3` works in every shell the agent opens, regardless of PATH.
# The distro's pip/setuptools/wheel are Debian-packaged and have no RECORD
# file, so a plain --upgrade fails trying to uninstall them. --ignore-installed
# lays fresh copies over the top in /usr/local instead.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --break-system-packages --ignore-installed setuptools wheel \
    && python3 -m pip install --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# cvxpy pulls in highspy transitively, and highspy's libhighs exports symbols
# that clash with the HiGHS bundled inside libortools.so - once highspy is
# imported, pywraplp and CP-SAT fail to load at all. Removing it costs nothing:
# HiGHS is still reachable via pywraplp.Solver.CreateSolver("HIGHS") and
# scipy.optimize.linprog, and cvxpy keeps Clarabel, OSQP, SCS and SCIPY.
RUN python3 -m pip uninstall -y --break-system-packages highspy

# Fail the build rather than the demo if a backend is missing. Every library is
# imported BEFORE any solver is instantiated: native symbol clashes between
# packages (highspy vs the HiGHS inside libortools) only surface in that order,
# and importing ortools first would hide them.
RUN python3 - <<'PY'
import cvxpy, duckdb, gurobipy, matplotlib, networkx, numpy, pandas
import polars, pulp, pyarrow, pyomo.environ, scipy
from ortools.linear_solver import pywraplp
from ortools.sat.python import cp_model

for name in ("SCIP", "CBC", "GLOP", "HIGHS"):
    assert pywraplp.Solver.CreateSolver(name) is not None, f"missing MIP backend: {name}"

model = cp_model.CpModel()
flag = model.NewBoolVar("flag")
model.Add(flag == 1)
assert cp_model.CpSolver().Solve(model) == cp_model.OPTIMAL, "CP-SAT cannot solve"

# DuckDB is the canonical storage format; prove it round-trips before shipping.
con = duckdb.connect()
assert con.sql("SELECT 42 AS answer").fetchone()[0] == 42
con.close()

print("solver stack OK:", pandas.__version__, numpy.__version__, duckdb.__version__)
PY

ENV PYTHONPATH=/workspace/src \
    RISKON_DATA_DIR=/workspace/data \
    RISKON_RUN_DIR=/workspace/runs
