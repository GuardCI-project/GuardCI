# Contributing

Use Linux, Python 3.10+ and Git 2.28+. Create a virtual environment and install
with python -m pip install ".[dev]". Do not commit environments, credentials,
datasets, design outputs, caches or private design inputs.

Update docs/DESIGN.md before changing behavior. Preserve the tool-independent
core and add regression tests. Do not weaken acceptance criteria to make a test
pass; keep unknown evidence distinct from proven validity.

Run pytest -q, ruff check . and python -m build. Install the resulting wheel into
a fresh environment, change outside the source tree, and run
scripts/check_distribution.py and scripts/smoke_git_ci.py. Syntax compilation is
also checked; the project does not currently configure a static type checker.

Optional Tcl tests need tclsh. Simulation regressions use host iverilog/vvp or
GUARDCI_TEST_IMAGE. For Docker tests, GUARDCI_TEST_CPUS is a comma-separated CPU
list and GUARDCI_TEST_LOCK_DIR selects the shared allocation directory. Only
use authorized resources. Never modify unrelated containers or global Git
identity settings.

For code ported from elsewhere, retain provenance and obtain a compatible
license or rights-holder permission before publishing it.
