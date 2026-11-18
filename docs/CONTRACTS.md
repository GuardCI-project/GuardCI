# Contract schema

A contract is a YAML mapping. `extends` pulls in a parent pack (path
relative to the file, or a builtin name like `orfs-default`).

| field | type | meaning |
|---|---|---|
| `name` | string | pack name (defaults to the file stem) |
| `extends` | string \| null | parent pack; merge rules below |
| `protected` | list of globs | read-only paths (fnmatch, `**` ok) |
| `recipe` | list of globs | paths a commit may change |
| `verification` | map | `{mode: full\|routed, source: artifact\|rebuild, fallback: rebuild\|none, superseded: verify\|defer\|skip, void: descendants\|until_restored}` . `void` (default `descendants`): whether a node rejected by an invariant voids every descendant permanently, or only until the cumulative delta is clean again |
| `protection` | map | `{mode: default\|explicit, rel_tol: 0.05}` — `default` (the default rule): every measurable protected dimension is an implicit hard no-regression edge `edge:<name>` unless listed in `tradable`; an `edges:` entry on a protected name replaces its params/severity, other `edges:` entries stay as extra ratchets. `explicit`: `edges:` is the whole ratchet list |
| `tradable` | list of strings | protected dimensions the owner lets get worse; unknown names are a `ContractError`. Valid names: `min_period`, `cell_area`, `power_total`, `ir_drop_worst`, `hold_wns` (clamped at 0), `electrical_violations`, `drc_count`, `antenna_violations`, `unconstrained_endpoints`, `unrouted_nets`, `tapcell_density`, `endcap_density`, `pdn_density` |
| `clauses` | list of maps | `{check: <name>, id: <optional>, kind: invariant|exit, severity: hard|warn|info|route, ...params}` |
| `predict` | map | `{stage, skip_below}` — predicted-open (off by default) |
| `edges` | list of maps | `{metric: <name>, severity, ...params}` — child vs parent |
| `routing` | map tag → list of check names | which clauses a delta tag triggers; the literal `heavy` means "all clauses whose checker cost is heavy" |

Merge rules (child over parent): `protected`/`recipe` append; `clauses`
override by `check` plus optional `id`; `routing` overrides by tag; `edges` override by
`metric`; `protection` merges by key; `tradable` appends.

Hard invariant failures produce violation; hard exit-criterion failures produce
unfinished. Warning-only findings may still be accepted; info findings annotate.
Route findings request the heavy check set. Check public verdict and coverage,
not only internal state names.

Builtin packs: `orfs-default` (checkpoint verification — `source:
artifact`, `fallback: none`; invariants `recipe_scope`/`library_closure`/
`equivalence`; `protection.mode: default` with nothing tradable;
`void: descendants`), `orfs-rebuild` (hermetic — the CI executes the
recipe, so `verification: {source: rebuild, fallback: rebuild}`; it
extends `orfs-default` unchanged otherwise — the same invariants,
default protection and `void: descendants` apply),
`phb-pa80` / `phb-pa80-rebuild` (the recorded tasks' 0.8× baseline
period + area targets on top of each; `phb-pa80` declares
`tradable: [power_total]`).

The phb-pa80-replay pack is an optional compatibility policy with
void: until_restored; it is not the default permanent-descendant policy.
