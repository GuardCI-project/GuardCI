# Provenance and third-party notices

GuardCI is distributed under the MIT License in LICENSE. The project owner has
confirmed ownership of GuardCI and ppa-hack-bench and authorized the GuardCI
release, including the ports listed below, under MIT. This does not change the
license of separately distributed third-party software or of ppa-hack-bench as
a whole.

Source comments identify ports from ppa-hack-bench in:
- guardci/executors/cpuset.py (CPU locking/allocation);
- guardci/flows/orfs/config.py (Make configuration parsing);
- guardci/flows/orfs/sdc.py (SDC parsing);
- guardci/flows/orfs/build.py (SDC parameterization);
- guardci/backends/klayout.py (DRC execution and report parsing);
- guardci/backends/kepler_formal.py (logic-equivalence invocation);
- guardci/backends/openroad/__init__.py (measurement and DEF sanitization);
- guardci/backends/openroad/measure.tcl (reference-period search).

The optional guardci/replay/phb.py reader and named phb-pa80 contract packs are
kept as interfaces/examples; no recorded episodes or research data are bundled.

PyYAML, Click and development dependencies are installed separately and retain
their own licenses. EDA tools, ORFS inputs, PDKs and validator images are not
included; their licenses and distribution conditions must be checked separately.
Preserve provenance and required notices when adding third-party code or fixtures.
