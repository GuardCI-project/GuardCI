# Operating Git + continuous verification

## Owner and author separation

The owner controls the contract, baseline, bare remote, ledger, caches and
validator. The author can submit Git commits and inspect approved status data.
A same-user local clone is a demonstration, not multi-user isolation. Configure
SSH authorization and OS-level permissions separately before exposing a service.
Never expose the Docker socket or owner project directory to untrusted authors.

## Initialization and resources

Follow README.md for installation and initialization. orfs-rebuild verifies
ordinary Git recipes by rebuilding; orfs-default expects trusted checkpoint
artifacts. Review the generated guardci.yaml before starting the worker:
executor image, slots, cpus_per_slot, memory, affinity and lock_dir.

Select CPU IDs that exist on your host and are allocated to this service. All
cooperating processes must use the same lock directory. Set artifacts_keep to
reports if full build workspaces are unnecessary, and set a cache limit
appropriate to available disk space. Do not copy another server's affinity.

Run guardci serve --project ABSOLUTE_PROJECT in a dedicated terminal or under
an owner-managed supervisor. The hook queues commits while the worker is down;
restart against the same project to resume. serve --once is a debugging command,
not the continuous service.

## Status and recovery

Query guardci status FULL_SHA --json --project ABSOLUTE_PROJECT. Poll with a
bounded timeout. Do not interpret the CLI exit code or successful push as a pass.
status --wait waits for a decision; combining --wait and --json emits successive
snapshots rather than one JSON document.

Only accepted with full/signoff coverage is delivery-eligible. Unfinished,
violation, void, unknown and pending are not accepted deliveries. Unknown checks
may leave a node verifying; retain the reason and investigate infrastructure or
evidence gaps without overriding the contract.

After an invariant violation, branch from a non-violating ancestor and push the
new branch. Do not force-push to erase provenance. Failed commits remain in Git
and in the ledger.

## Delivery and maintenance

Run guardci deliver --out NEW_DIRECTORY --project ABSOLUTE_PROJECT as the owner.
Verify MANIFEST.json's node, listed product hashes and verdict.json. Selection may
prefer a prior accepted commit on another branch. Do not use --no-rebuild for
production delivery: it is a candidate-inspection option.

Back up the Git remote, project configuration, contracts, ledger and required
artifacts together using a coordinated/quiesced snapshot. Supervision, upgrade
policy, backups and remote authentication are operator responsibilities.

## Scope of the bundled demo

scripts/smoke_git_ci.py uses real Git, a post-receive hook, a persistent worker
and status queries with a static-only contract. It proves the integration path,
not EDA signoff. Real-tool qualification requires your actual tool image,
licensed inputs, positive controls and known-invalid designs.
