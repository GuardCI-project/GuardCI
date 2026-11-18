"""Four-state regressions against the generated production testbench.

Uses host Icarus, or the opt-in GUARDCI_TEST_IMAGE container.
"""
import os
import re
import shutil
import subprocess

import pytest

from guardci.backends.equivalence import _generate_tb


@pytest.fixture
def simulate(tmp_path):
    image = os.environ.get("GUARDCI_TEST_IMAGE")
    host = shutil.which("iverilog") and shutil.which("vvp")
    if not image and not host:
        pytest.skip("Icarus not installed; set GUARDCI_TEST_IMAGE for Docker")

    def run(gold, revised, *, width=0, seed=1, cycles=5,
            probe=False, with_trace=False):
        ports = {"clk": {"direction": "input", "bits": 1},
                 "reset": {"direction": "input", "bits": 1},
                 "a": {"direction": "output", "bits": 2},
                 "b": {"direction": "output", "bits": 2}}
        if width:
            ports["payload"] = {"direction": "input", "bits": width}
        tb = _generate_tb("golden", ports,
                          {"port": "reset", "active": 1, "cycles": 2},
                          "clk", cycles, seed)
        modules = []
        for name, values in (("golden", gold), ("revised", revised)):
            data_port = f", input [{width-1}:0] payload" if width else ""
            a = f"2'b{values[0]}"
            if probe and name == "revised":
                a = ("{1'b0, (payload[63:32] != 32'b0 && "
                     "payload[63:32] != 32'hffffffff)}")
            modules.append(
                f"module {name}(input clk, reset{data_port}, output [1:0] a, b);\n"
                f"assign a = {a}; assign b = 2'b{values[1]};\n"
                "endmodule\n")
        if image:
            from guardci.executors.docker import DockerExecutor
            (tmp_path / "tb.v").write_text(tb + "\n" + "".join(modules))
            executor = DockerExecutor(
                image=image, slots=1, cpus_per_slot=2,
                affinity=[int(cpu) for cpu in os.environ["GUARDCI_TEST_CPUS"].split(",")]
                if os.environ.get("GUARDCI_TEST_CPUS") else None,
                lock_dir=os.environ.get("GUARDCI_TEST_LOCK_DIR", "/tmp/guardci-cpusets"))
            slot = executor.acquire()
            try:
                result = executor.run(
                    ["bash", "-lc",
                     ("iverilog -s tb -o /out/test.vvp /out/tb.v && "
                      "vvp /out/test.vvp")],
                    mounts={str(tmp_path): ("/out", "rw")},
                    slot=slot, timeout=30, log=tmp_path / "sim.log")
                assert result.rc == 0, (tmp_path / "sim.log").read_text()
            finally:
                slot.release()
            log = (tmp_path / "sim.log").read_text()
        else:
            tb = tb.replace("/out/stimulus.txt", str(tmp_path / "stimulus.txt"))
            (tmp_path / "tb.v").write_text(tb + "\n" + "".join(modules))
            subprocess.run(
                ["iverilog", "-s", "tb", "-o", str(tmp_path / "test.vvp"),
                 str(tmp_path / "tb.v")], check=True, capture_output=True)
            log = subprocess.run(
                ["vvp", str(tmp_path / "test.vvp")], check=True,
                capture_output=True, text=True).stdout
        result = re.search(
            r"RESULT mismatch=(\d+) cycle=(\d+) compared=(\d+) xcycles=(\d+)",
            log)
        assert result, log
        parsed = tuple(map(int, result.groups()))
        if with_trace:
            return parsed, (tmp_path / "stimulus.txt").read_text()
        return parsed

    return run


@pytest.mark.parametrize("gold,revised", [
    (("x0", "00"), ("x1", "00")),
    (("00", "xx"), ("01", "xx")),
    (("z0", "zz"), ("z1", "zz")),
    (("0x", "00"), ("1z", "00")),
    (("00", "00"), ("01", "00")),
])
def test_defined_difference_refutes_despite_unknown_outputs(simulate, gold, revised):
    mismatch, cycle, compared, _ = simulate(gold, revised)
    assert (mismatch, cycle, compared) == (1, 1, 0)


@pytest.mark.parametrize("gold,revised", [
    (("xx", "zz"), ("zz", "xx")),
    (("x0", "00"), ("00", "00")),
    (("00", "00"), ("x0", "00")),
    (("z0", "00"), ("10", "00")),
    (("00", "xx"), ("00", "xx")),
])
def test_unknown_alone_neither_refutes_nor_counts(simulate, gold, revised):
    assert simulate(gold, revised) == (0, 5, 0, 5)


def test_fully_defined_equal_cycles_still_count(simulate):
    assert simulate(("10", "01"), ("10", "01")) == (0, 5, 5, 0)


def test_seed_is_reproducible_and_changes_stimulus(simulate):
    def trace(seed):
        return simulate(("00", "00"), ("00", "00"), width=64,
                        seed=seed, cycles=32, with_trace=True)[1]
    assert trace(7) == trace(7)
    assert trace(7) != trace(11)


@pytest.mark.parametrize("width", [1, 31, 32, 33, 64, 65, 129])
def test_all_input_bits_are_exercised(simulate, width):
    result, trace = simulate(("00", "00"), ("00", "00"), width=width,
                             seed=7, cycles=128, with_trace=True)
    assert result == (0, 128, 128, 0)
    values = [int(x, 16) for x in re.findall(r"payload=([0-9a-f]+)", trace)]
    for bit in range(width):
        assert {v >> bit & 1 for v in values[1:]} == {0, 1}
    for offset in range(0, width - 31, 32):
        assert len({v >> offset & 0xFFFFFFFF for v in values[1:]}) > 2


def test_wide_input_mutation_is_detected_and_failing_stimulus_logged(simulate):
    result, trace = simulate(("00", "xx"), ("00", "xx"),
                             width=64, probe=True, with_trace=True)
    assert result[0] == 1
    samples = re.findall(r"cycle (\d+) payload=([0-9a-f]+)", trace)
    cycle, value = samples[-1]
    assert int(cycle) == result[1]
    assert int(value, 16) >> 32 not in (0, 0xFFFFFFFF)
    assert "a gold=00 revised=01" in trace
    assert "b gold=xx revised=xx" in trace

