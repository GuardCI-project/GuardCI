# GuardCI architecture and release contract

This document is the implementation specification for the standalone release.
Update it before changing behavior. The research workspace and recorded experiments
are not required by, or distributed with, this source release.

## Trust boundary and lifecycle

The owner configures a protected baseline, a contract, a bare Git remote and a
separate verifier. Authors submit recipe commits. A post-receive hook records
each commit and queues verification; a successful push is not acceptance.
The worker classifies changes, checks protected inputs, builds/measures artifacts
when required and records results in SQLite, Git notes and status refs.

The owner must protect configuration, database, caches and golden inputs from
author writes. Git transport authorization and operating-system isolation are
deployment responsibilities. Docker resource limits and disabled networking do
not constitute a complete adversarial sandbox.

## Verdicts and evidence

Public verdicts are accepted, unfinished, violation and void. Internal ledger
states (including verified, warned, open, rejected and tainted) are implementation
details; clients inspect verdict and coverage together. Pending/verifying results
are not acceptance. An initialization reference is not an accepted full delivery.

Invariant failures invalidate a design or its permitted lineage. Unmet exit
requirements remain unfinished. Warnings do not by themselves prevent acceptance.
Unknown measurements and bounded evidence must not be described as proofs.

The default descendant policy records invalid ancestry. Recovery starts from an
eligible ancestor on a new branch rather than deleting history.
Only signoff-accepted candidates are eligible for delivery. The delivery operation
rebuilds/validates the selected candidate and writes real design products,
verdict.json and a SHA-256 MANIFEST.json. The selected node need not be HEAD.

## Components

- core: ledger, graph, scheduler, cache, verification and delivery.
- contract: YAML validation, inheritance, clauses and protected dimensions.
- flows: recipe classification, build stages and artifact mapping.
- checks/backends: invariants, exit criteria and independent tool measurements.
- executors: resource allocation and container execution.
- remote/cli: Git ingress, owner commands and read-only status inspection.
- recorders: trusted checkpoint capture for checkpoint-based verification.

Layering is enforced by tests/test_layering.py. Tool-specific logic stays in
flows, backends and executors. Plugin interfaces and built-in registrations are
defined in their respective base modules. The optional replay module is retained
for API compatibility; no datasets or experiment runners are included.

## Contracts and packaging

Built-in YAML packs live in guardci/contract/packs and ship in both wheel and
source distributions. Resolution tries an explicit/relative file first, then a
built-in name; inheritance uses the same rule. It must work outside a checkout.

Distributions also contain ORFS variable metadata, OpenROAD Tcl scripts and the
extra Verilog cell models used by equivalence checks. Tests must verify these
assets in a freshly installed wheel, not only an editable checkout.

The release retains existing acceptance rules and cache-key semantics. No adapter
version bump is needed for resource relocation alone. Changes to build attribution
or config parsing require an ADAPTER_VERSION bump and cache-soundness validation.

## External tools and test scope

The null flow supports static Git lifecycle demonstrations without EDA tools;
it does not measure timing, area, DRC or equivalence. Real ORFS verification
requires an owner-provided compatible validator image and licensed design/PDK
inputs. See VALIDATOR.md and OPERATIONS.md. No image, PDK or proprietary tool is
bundled, and this release does not promise public availability of the development
validator image.

Optional simulation tests use host Icarus or a caller-supplied Docker image.
Docker tests accept host CPU affinity and lock-directory settings through
environment variables; no development-server CPU IDs are embedded.

This is a Linux, single-host research prototype, not a production hosted Git
service or a claim of complete signoff coverage for arbitrary designs.
