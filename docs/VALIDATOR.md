# External validator requirements

GuardCI's Python package does not bundle EDA tools, PDKs, design inputs or a
container image. The development tag flowproof/validator:locked was used in the
research environment; its public availability and redistribution rights have
not been established. Do not treat docker pull of that tag as an installation
instruction. Releasing a reproducible validator build is a separate prerequisite
for a turnkey public real-EDA quick start.

The current adapter expects a compatible Linux image with:
- OpenROAD and OpenSTA under /OpenROAD-flow-scripts/tools/install/OpenROAD/bin;
- Yosys and its simulation cells under /OpenROAD-flow-scripts/tools/install/yosys;
- kepler-formal under /OpenROAD-flow-scripts/tools/install/kepler-formal/bin;
- KLayout, Icarus Verilog, Python, Make, Tcl and standard shell utilities;
- an ORFS-compatible toolchain for the submitted flow tree and technology.

guardci/backends/toolenv.py defines the executable binding. The executor mounts
submitted inputs, protected reference inputs and outputs at fixed container paths.
It runs without networking, as the invoking uid:gid, with configured CPU/memory
limits. The image must support these permissions and paths.

Provide an approved local image via --image, preferably referenced by digest.
Check its provenance and all bundled tool/PDK licenses before redistribution.
A locally verified image is not automatically a redistributable image. Pin and
record your toolchain and independently validate baseline metrics; do not reuse
published measurements as your own acceptance baseline.

No claim is made that the bundled null-flow smoke test substitutes for this
toolchain, physical verification or formal equivalence.
