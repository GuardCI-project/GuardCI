# Release checklist

Version 0.0.1 is a research-prototype source release candidate. Source publication
does not imply a release tag, PyPI upload or a publicly distributed EDA image.

## Source publication gates

- [x] The owner confirmed ownership of GuardCI and ppa-hack-bench and authorized
  MIT distribution of this release, including its ports and project fixtures.
- [x] Preserve provenance and any required third-party notices.
- [x] Publish under GuardCI-project at https://github.com/GuardCI-project/GuardCI.
- [x] GitHub private vulnerability reporting is enabled; its reporting link is
  documented in SECURITY.md.

## Technical release gate

- Run the full source tests and lint; report optional skips.
- Build wheel and sdist, then install each outside the source checkout.
- Confirm built-in contracts, Tcl scripts, variable metadata and Verilog cells
  are included, and package metadata and LICENSE agree on MIT.
- Run the real Git + continuous-worker static smoke test from each installation.
- Scan source and archives for credentials, private paths, caches and internal data.
- Ensure no original Git history, experiment outputs or private papers are included.
- Record the local validation results before tagging; GitHub Actions CI is not configured.

## External EDA qualification (not bundled)

For a turnkey real-EDA release, provide a licensed, reproducible validator
image/build procedure and qualify operation with licensed design inputs.
Current public image availability is unverified. No EDA tools, PDKs, images or
research episodes are distributed in this source release.

A local passing test report does not resolve licensing or deployment blockers.
