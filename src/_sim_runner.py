"""Standalone helper invoked as a subprocess by simulate_testbench in tools.py
— never imported directly. Kept as a separate process (not called in-process
via the cocotb_tools API directly from tools.py) for the same reason every
other tool here shells out: a hard timeout via subprocess.run, and isolation
from whatever state a hung or crashed simulation leaves behind.

Usage: python _sim_runner.py <test_file.py> <stub_dut.sv> <build_dir>

Prints a clean, parsed summary to stdout and exits 0 if every test in
test_file.py passed against stub_dut.sv, 1 otherwise (including on a build
failure or any exception raised outside an individual @cocotb.test()).
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    test_file, stub_sv, build_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

    # Imported here, not at module level — this script only runs inside the
    # subprocess simulate_testbench spawns, and importing cocotb_tools at
    # module scope would make every other tools.py import pay that cost.
    from cocotb_tools.runner import get_runner

    runner = get_runner("icarus")
    try:
        runner.build(
            sources=[str(stub_sv)], hdl_toplevel="TopModule",
            build_dir=str(build_dir), always=True,
        )
    except Exception as e:
        # A build failure here means the auto-generated stub itself is
        # broken (a bug in simulate_testbench's stub generation, not in the
        # model's testbench) — surfaced distinctly so it's never confused
        # with a real testbench problem.
        print(f"STUB_BUILD_ERROR: {type(e).__name__}: {e}")
        sys.exit(1)

    results_xml = build_dir / "results.xml"
    try:
        runner.test(
            hdl_toplevel="TopModule", test_module=test_file.stem,
            test_dir=str(test_file.parent), build_dir=str(build_dir),
            results_xml=str(results_xml),
        )
    except Exception as e:
        # cocotb's runner.test() normally reports per-test failures via
        # results.xml (parsed below) without raising — an exception here
        # means something broke before individual tests could even run
        # (e.g. the test module itself failed to import).
        print(f"SIMULATION_ERROR: {type(e).__name__}: {e}")
        sys.exit(1)

    if not results_xml.exists():
        print("SIMULATION_ERROR: no results.xml was produced.")
        sys.exit(1)

    root = ET.parse(results_xml).getroot()
    testcases = root.findall(".//testcase")
    if not testcases:
        print("SIMULATION_ERROR: results.xml contains no test cases.")
        sys.exit(1)

    any_failed = False
    for tc in testcases:
        name = f"{tc.get('classname')}.{tc.get('name')}"
        failure = tc.find("failure")
        if failure is None:
            print(f"PASS: {name}")
        else:
            any_failed = True
            print(f"FAIL: {name}: {failure.get('error_type')}: {failure.get('error_msg')}")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
