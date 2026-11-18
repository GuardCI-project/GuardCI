"""Container layout and tool environment shared by the ORFS flow
adapter and the measurement backends (DESIGN.md §9.9: tool binding
belongs to the executor/CI, not the recipe).

Kept in a neutral module so ``backends/`` never imports ``flows.*``.
"""

CONTAINER_FLOW = "/work/flow"
CONTAINER_GUARDCI = "/guardci"
CONTAINER_GOLDEN = "/golden/flow"
CONTAINER_OUT = "/out"

TOOLBINS = "/OpenROAD-flow-scripts/tools/install"
TOOL_ENV = {
    "PATH": f"{TOOLBINS}/kepler-formal/bin:{TOOLBINS}/yosys/bin:"
            f"{TOOLBINS}/OpenROAD/bin:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin",
    "YOSYS_EXE": f"{TOOLBINS}/yosys/bin/yosys",
    "OPENROAD_EXE": f"{TOOLBINS}/OpenROAD/bin/openroad",
    "OPENSTA_EXE": f"{TOOLBINS}/OpenROAD/bin/sta",
    "KEPLER_FORMAL_EXE": f"{TOOLBINS}/kepler-formal/bin/kepler-formal",
}
# EXE vars also go on the make command line (§9.3: tool binding belongs to
# the executor/CI, overriding anything a recipe assigns)
EXE_VARS = ("YOSYS_EXE", "OPENROAD_EXE", "OPENSTA_EXE", "KEPLER_FORMAL_EXE")
