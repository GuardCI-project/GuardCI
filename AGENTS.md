# Contributor guidance

Read docs/DESIGN.md before implementation and update it first for behavior changes.
Run pytest -q, ruff check . and distribution smoke checks after changes.
Keep tool names/logic out of the core; tests/test_layering.py enforces layering.
Do not weaken checks, mutate unrelated projects or publish credentials/data.
Resource allocation is operator-specific: never hard-code development-host CPUs.
Do not publish or tag until RELEASE_CHECKLIST.md blockers are resolved.
