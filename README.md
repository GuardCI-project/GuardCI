# GuardCI

A Git remote with continuous verification for autonomous EDA.

Authors push implementation recipes; an owner-controlled worker checks the
protected design and contract. A successful push queues work — it does not mean
the design passed. GuardCI is a single-host research prototype.

## Install from source

Requirements: Linux, Python 3.10+, Git 2.28+.

```sh
git clone https://github.com/GuardCI-project/GuardCI.git
cd GuardCI
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
guardci --help
```

The source distribution and wheel are installable without the original research
workspace. No experiment data, model credentials, PDKs or tool binaries are bundled.
This is a release candidate, not a package already published on PyPI.

## Try the Git + CI loop without EDA tools

```sh
python scripts/smoke_git_ci.py --root /absolute/path/to/new-smoke-directory
```

This starts a continuous worker before real Git pushes, checks a legal recipe,
rejects a protected-input change, observes invalid descendants, and recovers on
a clean branch. It uses a static-only contract and the null flow: it is **not**
physical-design signoff. The script retains its evidence and stops its own worker.

## Real design verification

First provision a compatible validator image and an ORFS flow tree; see
[validator requirements](docs/VALIDATOR.md). A development image tag is not a
public installation channel.

```sh
PROJECT="$PWD/my-ci-project"
guardci init "$PROJECT" --contract orfs-rebuild --flow orfs \
  --platform nangate45 --design gcd --reset-port reset \
  --from /absolute/path/to/flow --image YOUR_VALIDATOR_IMAGE \
  --slots 1 --cpus-per-slot 4 --memory 16g
# Review affinity, locks and resource limits for your host before starting.
guardci serve --project "$PROJECT"
```

In another terminal, using the same absolute PROJECT path:

```sh
git clone "$PROJECT/remote.git" work
cd work
# Make a permitted recipe change; use your normal Git author identity.
git add designs/nangate45/gcd/config.mk
git commit -m "Improve implementation recipe"
git push origin HEAD:main
guardci status "$(git rev-parse HEAD)" --json --project "$PROJECT"
```

Poll the exact commit until terminal. Only accepted with full/signoff coverage
is eligible for delivery. The owner exports with:

```sh
guardci deliver --out /absolute/path/to/new-delivery --project "$PROJECT"
```

Inspect the manifest's selected node and hashes; selection need not equal HEAD.

## Verdicts

| Verdict | Meaning |
|---|---|
| accepted | Applicable contract requirements satisfied; warnings may remain. |
| unfinished | Exit requirements unmet; not deliverable. |
| violation | An invariant failed; not deliverable. |
| void | Invalid ancestry under the configured policy; not deliverable. |

Pending, unknown or verifying results are not passes. A reference baseline's
internal state is not sufficient evidence of delivery eligibility.

## Development and documentation

```sh
python -m pip install ".[dev]"
pytest -q
ruff check .
python -m build
```

Host Tcl and Icarus enable optional script/simulation regressions; skipped tests
are reported explicitly. See [contributing](CONTRIBUTING.md),
[operations](docs/OPERATIONS.md), [contracts](docs/CONTRACTS.md),
[architecture](docs/DESIGN.md), and [security](SECURITY.md).

## Licensing and release status

GuardCI is licensed under [MIT](LICENSE), including the ppa-hack-bench ports
authorized by their owner. Preserve [provenance notices](THIRD_PARTY_NOTICES.md).
The source repository is [GuardCI-project/GuardCI](https://github.com/GuardCI-project/GuardCI).

Version 0.0.1 is a research-prototype release candidate, not a turnkey EDA service.
External EDA tools, PDKs and validator images are not distributed here and require
separate licensing and setup. See the [release checklist](RELEASE_CHECKLIST.md)
before creating a release tag or publishing packages to a registry.
