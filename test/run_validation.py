import argparse
import fnmatch
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows terminals default to a codepage that can't render some characters —
# force UTF-8 output so nothing gets garbled (same fix as testbench_agent.py
# / run_benchmark.py).
sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Matches one declared port from a verilog-eval *_prompt.txt line, e.g.
# " - input  clk", " - output q (4 bits)", " - input  sel (   8 bits)" —
# confirmed directly across several prompts (Prob001_zero, Prob021_mux256to1v,
# Prob109_fsm1) that every port is declared this way, one per line, so this
# is a reliable extraction, not a guess at the format.
PORT_DECL_RE = re.compile(r"^\s*-\s+(?:input|output)\s+(\w+)", re.MULTILINE)

# Matches every dut.<name> reference in a generated testbench.
DUT_REF_RE = re.compile(r"\bdut\.(\w+)\b")

# cocotb's own SimHandleBase surface — legitimate dut.<x> attribute
# accesses that are never a port name, so they must not be flagged by the
# port-mismatch heuristic below. Not exhaustive by construction (enumerated
# from cocotb's documented dut object surface, not discovered empirically
# from false positives) — extend if a real testbench legitimately uses
# something not on this list.
DUT_INTERNAL_ATTRS = {"_log", "_name", "_path", "_id", "_handle", "_sub_handles"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Re-check each problem's generated cocotb testbench (from a "
                    "run_benchmark.py output directory) independently of the agent's own "
                    "transcript, and flag likely port-name mismatches against the problem's "
                    "declared interface. Companion to run_benchmark.py: that script "
                    "generates code, this one re-checks it — kept separate since generation "
                    "and validation are different concerns run at different times, and "
                    "because an independent re-run can catch a stale check (the agent edited "
                    "a file after its last check_testbench call) that the live transcript "
                    "alone wouldn't reveal.\n\n"
                    "IMPORTANT: this is NOT a correctness signal. There is no reference RTL "
                    "to run the testbench against (the whole point of this agent is writing "
                    "testbenches for modules that don't exist yet), so 'clean' only means "
                    "the file is well-formed Python that defines at least one real test — it "
                    "says nothing about whether the testbench actually drives or checks the "
                    "right behavior. 'port_mismatch_suspected' is a heuristic, not a proof."
    )
    parser.add_argument(
        "--run-dir", required=True,
        help="A run_benchmark.py output directory (e.g. benchmark_runs/<timestamp>), "
             "containing <config-stem>/<problem>/generated/ subdirectories.",
    )
    parser.add_argument(
        "--dataset-dir", required=True,
        help="Path to a cloned verilog-eval repo's dataset_spec-to-rtl/ directory (needs "
             "<problem>_prompt.txt to extract the declared port list for the "
             "port_mismatch_suspected check — _ref.sv/_test.sv are never read).",
    )
    parser.add_argument(
        "--configs", nargs="+", default=None,
        help="Config-stem subdirectory names under --run-dir to validate (default: all found).",
    )
    parser.add_argument(
        "--problems", nargs="*", default=None,
        help="Exact problem names or fnmatch-style globs (e.g. 'Prob00*') to filter the set. "
             "Default: all problems found under each config.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-validate a problem even if validation.json already exists. Default: skip it.",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="Per-file ruff subprocess timeout in seconds (default: 15, same as "
             "check_testbench's own timeout in tools.py).",
    )
    return parser.parse_args()


def discover_config_dirs(run_dir: Path) -> list[Path]:
    # run_dir also contains command.txt and a configs/ subdir (copies of the
    # YAML files used) alongside the actual <config-stem>/ problem-set dirs —
    # exclude those, they're not something to validate.
    return sorted(
        d for d in run_dir.iterdir()
        if d.is_dir() and d.name != "configs"
    )


def discover_problem_dirs(config_dir: Path) -> list[Path]:
    return sorted(d for d in config_dir.iterdir() if d.is_dir())


def find_generated_files(problem_dir: Path) -> list[Path]:
    gen_dir = problem_dir / "generated"
    if not gen_dir.exists():
        return []
    return sorted(gen_dir.rglob("*.py"))


def check_file_static(path: Path, timeout: float) -> str | None:
    # Same three checks as check_testbench in src/tools.py, re-run
    # independently here rather than trusted from the transcript — returns
    # None if clean, or a short reason string if not. Kept in sync with
    # tools.py deliberately rather than importing it, since this script's
    # only dependency should be the generated *.py file on disk, not the
    # agent's own src/ package (mirrors run_benchmark.py's existing
    # sys.path.insert pattern only for config/tools, not for tools.py's
    # actual check logic, to keep this an independent re-implementation).
    text = path.read_text(encoding="utf-8")

    try:
        compile(text, str(path), "exec")
    except SyntaxError as e:
        return f"SyntaxError: {e.msg} (line {e.lineno}, column {e.offset})"

    if "@cocotb.test" not in text:
        return "no @cocotb.test() function found"
    if "assert" not in text:
        return "no assert statement found"

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "F", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        return (result.stdout + result.stderr).strip()
    return None


def parse_declared_ports(prompt_path: Path) -> set[str] | None:
    if not prompt_path.exists():
        return None
    text = prompt_path.read_text(encoding="utf-8")
    ports = set(PORT_DECL_RE.findall(text))
    return ports or None  # empty match set is as unparseable as no file


def find_port_mismatches(generated_files: list[Path], declared_ports: set[str]) -> list[str]:
    referenced = set()
    for f in generated_files:
        referenced |= set(DUT_REF_RE.findall(f.read_text(encoding="utf-8")))
    suspicious = referenced - declared_ports - DUT_INTERNAL_ATTRS
    return sorted(suspicious)


def validate_problem(problem: str, problem_dir: Path, dataset_dir: Path, timeout: float) -> dict:
    result = {"problem": problem, "timestamp": datetime.now(timezone.utc).isoformat()}
    start = datetime.now(timezone.utc)

    generated_files = find_generated_files(problem_dir)
    if not generated_files:
        result["status"] = "no_generated_code"
        result["duration_s"] = round((datetime.now(timezone.utc) - start).total_seconds(), 2)
        return result

    errors = {}
    for f in generated_files:
        reason = check_file_static(f, timeout)
        if reason is not None:
            errors[f.name] = reason

    if errors:
        result["status"] = "static_errors"
        result["errors"] = errors
        result["duration_s"] = round((datetime.now(timezone.utc) - start).total_seconds(), 2)
        return result

    declared_ports = parse_declared_ports(dataset_dir / f"{problem}_prompt.txt")
    if declared_ports is None:
        result["status"] = "unparseable_ports"
        result["duration_s"] = round((datetime.now(timezone.utc) - start).total_seconds(), 2)
        return result

    mismatches = find_port_mismatches(generated_files, declared_ports)
    if mismatches:
        result["status"] = "port_mismatch_suspected"
        result["suspected_signals"] = mismatches
        result["declared_ports"] = sorted(declared_ports)
        result["duration_s"] = round((datetime.now(timezone.utc) - start).total_seconds(), 2)
        return result

    result["status"] = "clean"
    result["duration_s"] = round((datetime.now(timezone.utc) - start).total_seconds(), 2)
    return result


STATUS_MEANINGS = {
    "clean": "well-formed Python, defines a real test, references only declared port names",
    "static_errors": "the generated file(s) have a real problem: syntax error, undefined name, "
                      "no @cocotb.test(), or no assert",
    "no_generated_code": "no .py file was ever written for this problem",
    "port_mismatch_suspected": "clean Python, but references a dut.<signal> not in the "
                                "prompt's declared port list — likely (not certain) a typo'd "
                                "or hallucinated signal name",
    "unparseable_ports": "the prompt's port list couldn't be parsed, so port_mismatch_suspected "
                          "couldn't be checked",
}


def build_summary_report(run_dir: Path, dataset_dir: Path, summary: dict) -> dict:
    report = {
        "run_dir": str(run_dir),
        "dataset_dir": str(dataset_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "NOT a correctness signal — there is no reference RTL to run the testbench "
                "against. 'clean' only re-confirms Python well-formedness independently of "
                "the transcript; 'port_mismatch_suspected' is a heuristic, not a proof.",
        "status_meanings": STATUS_MEANINGS,
        "configs": {},
    }

    for config_name, by_status in summary.items():
        total = sum(len(v) for v in by_status.values())
        clean = len(by_status.get("clean", []))

        report["configs"][config_name] = {
            "total": total,
            "clean": clean,
            "clean_rate": round(clean / total, 4) if total else None,
            "static_errors": sorted(p for p, _ in by_status.get("static_errors", [])),
            "port_mismatch_suspected": [
                {"problem": p, "suspected_signals": r.get("suspected_signals")}
                for p, r in sorted(by_status.get("port_mismatch_suspected", []), key=lambda pr: pr[0])
            ],
            "no_generated_code": sorted(p for p, _ in by_status.get("no_generated_code", [])),
            "unparseable_ports": sorted(p for p, _ in by_status.get("unparseable_ports", [])),
        }

    return report


def print_summary(run_dir: Path, report: dict) -> None:
    print(f"\n{'=' * 60}")
    print("Validation Summary — NOT a correctness signal, see note below")
    print(f"{'=' * 60}")
    print(f"Run directory: {run_dir}")
    print(f"\n{report['note']}\n")

    for config_name, c in report["configs"].items():
        rate = f"{100 * c['clean_rate']:.0f}%" if c["clean_rate"] is not None else "n/a"

        print(f"\n--- {config_name} ---")
        print(f"Clean: {c['clean']}/{c['total']} ({rate})")

        if c["static_errors"]:
            print(f"\nStatic errors ({len(c['static_errors'])}) — real problems in the generated file:")
            for problem in c["static_errors"]:
                print(f"  {problem}")

        if c["port_mismatch_suspected"]:
            print(f"\nPort mismatch suspected ({len(c['port_mismatch_suspected'])}) — heuristic, not certain:")
            for f in c["port_mismatch_suspected"]:
                print(f"  {f['problem']} (references: {', '.join(f['suspected_signals'])})")

        if c["no_generated_code"]:
            print(f"\nNo generated code ({len(c['no_generated_code'])}):")
            for problem in c["no_generated_code"]:
                print(f"  {problem}")

        if c["unparseable_ports"]:
            print(f"\nUnparseable ports ({len(c['unparseable_ports'])}) — clean Python, but "
                  f"couldn't cross-check port names:")
            for problem in c["unparseable_ports"]:
                print(f"  {problem}")


def main():
    args = parse_args()

    run_dir = Path(args.run_dir).resolve()
    dataset_dir = Path(args.dataset_dir).resolve()

    if not run_dir.is_dir():
        sys.exit(f"--run-dir '{run_dir}' is not a directory.")
    if not dataset_dir.is_dir():
        sys.exit(f"--dataset-dir '{dataset_dir}' is not a directory.")

    config_dirs = discover_config_dirs(run_dir)
    if args.configs:
        config_dirs = [d for d in config_dirs if d.name in args.configs]
        if not config_dirs:
            sys.exit(f"--configs filter matched none of the config-stem directories under '{run_dir}'.")
    if not config_dirs:
        sys.exit(f"No config-stem subdirectories found under '{run_dir}'. Did you point at a run_benchmark.py output directory?")

    print(f"Run directory: {run_dir}")
    print(f"Dataset directory: {dataset_dir}")
    print(f"Configs: {[d.name for d in config_dirs]}\n")

    # config_name -> {status: [(problem, result_dict), ...]} — every problem
    # lands here regardless of whether it was freshly validated this run or
    # already had a validation.json from a previous run, so the final
    # summary always reflects the true current state of the whole run
    # directory, not just what changed in this invocation.
    summary = {}

    for config_dir in config_dirs:
        config_name = config_dir.name
        summary[config_name] = {}

        problem_dirs = discover_problem_dirs(config_dir)
        if args.problems:
            problem_dirs = [
                d for d in problem_dirs
                if any(fnmatch.fnmatch(d.name, pattern) or d.name == pattern for pattern in args.problems)
            ]

        for problem_dir in problem_dirs:
            problem = problem_dir.name
            validation_file = problem_dir / "validation.json"

            if validation_file.exists() and not args.overwrite:
                result = json.loads(validation_file.read_text(encoding="utf-8"))
                print(f"[skip] {config_name}/{problem} already validated ({result.get('status')})")
            else:
                result = validate_problem(problem, problem_dir, dataset_dir, args.timeout)
                validation_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(f"[{result['status']}] {config_name}/{problem} ({result.get('duration_s', 0):.1f}s)")

            summary[config_name].setdefault(result["status"], []).append((problem, result))

    report = build_summary_report(run_dir, dataset_dir, summary)
    summary_file = run_dir / "validation_summary.json"
    summary_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print_summary(run_dir, report)
    print(f"\nSummary written to: {summary_file}")


if __name__ == "__main__":
    main()
