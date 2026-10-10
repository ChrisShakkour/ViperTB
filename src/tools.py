import os
import re
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

# Where the agent's testbench project lives. Kept separate from the
# script's own directory so it can be freely cleared/gitignored without
# touching the actual agent code.
GENERATED_DIR = os.path.join(os.path.dirname(__file__), "generated")

# _sim_runner.py is a standalone script (not imported), invoked as a
# subprocess by simulate_testbench below for a hard timeout + isolation,
# same reasoning as every other subprocess call in this file.
SIM_RUNNER_PATH = os.path.join(os.path.dirname(__file__), "_sim_runner.py")

# Matches every dut.<name> reference in a generated testbench.
_DUT_REF_RE = re.compile(r"\bdut\.(\w+)\b")

# Matches dut.<name>.value = ... (an assignment, driving the signal from the
# testbench) but not dut.<name>.value == ... (a comparison) — the negative
# lookahead rejects a second "=" right after the first. Confirmed directly
# this correctly separates "assert dut.q.value == 0" (read) from
# "dut.clk.value = 0" (write) on real generated content.
_DUT_WRITE_RE = re.compile(r"dut\.(\w+)\.value\s*=(?!=)")

# cocotb's own SimHandleBase surface — legitimate dut.<x> attribute
# accesses that are never a real port name, so the stub generator must not
# try to declare them as ports. Not exhaustive by construction (enumerated
# from cocotb's documented dut object surface, not discovered empirically
# from a crash) — extend if a real testbench legitimately uses something
# not on this list.
_DUT_INTERNAL_ATTRS = {"_log", "_name", "_path", "_id", "_handle", "_sub_handles"}

# Parses the port-list comment/docstring the system prompt asks the model
# to restate at the top of every file — two patterns since real generated
# output has been observed using both "- name (direction, N bits)" and
# "- name: direction, N-bit" phrasing for the same information. This is
# NOT a bonus/fallback source — it is THE ground truth the placeholder
# module is built from. Confirmed directly that deriving the stub's ports
# from the testbench's own dut.<name> usage instead (the first version of
# this code did that) is circular and can never catch a wrong signal name:
# whatever the testbench references trivially "exists" in a stub generated
# from those same references. Cross-checking dut.<name> usage against this
# independently-declared list is what actually catches a typo — the same
# idea as run_validation.py's port_mismatch_suspected heuristic, just
# running live during generation instead of after the fact.
_PORT_DECL_RE_A = re.compile(r"-\s*(\w+)\s*\((input|output),?\s*(\d+)[\s-]*bits?\)", re.IGNORECASE)
_PORT_DECL_RE_B = re.compile(r"-\s*(\w+):\s*(input|output),\s*(\d+)[\s-]*bits?", re.IGNORECASE)


def _parse_declared_ports(text: str) -> dict[str, tuple[str, int]]:
    # -> {name: (direction, width)}. Pattern A checked first; pattern B
    # only fills in names A didn't already find, so the two never disagree
    # on the same signal.
    declared = {}
    for m in _PORT_DECL_RE_A.finditer(text):
        declared[m.group(1)] = (m.group(2).lower(), int(m.group(3)))
    for m in _PORT_DECL_RE_B.finditer(text):
        declared.setdefault(m.group(1), (m.group(2).lower(), int(m.group(3))))
    return declared


def _resolve_safe_path(path: str) -> Path:
    # Shared by every tool below. Resolves a model-supplied path against
    # GENERATED_DIR and rejects anything that would escape it (e.g. "../..",
    # or an absolute path) — the model's path arguments are untrusted input,
    # normalized and checked so nothing can escape the sandbox, while still
    # allowing safe subdirectories (needed for list_directory to browse a
    # project with multiple test files).
    os.makedirs(GENERATED_DIR, exist_ok=True)
    base = Path(GENERATED_DIR).resolve()

    # Models very predictably pass a path as if they need to name
    # GENERATED_DIR themselves — e.g. "/generated/foo.py" — because a
    # request like "put it under the generated directory" reads naturally
    # as "include 'generated' in the path". Two things go wrong if we don't
    # correct for this: a leading "/" makes pathlib's `base / path` discard
    # `base` entirely (joining with an absolute path replaces the whole
    # thing, resolving to the filesystem root instead of our sandbox), and
    # even a relative "generated/foo.py" would nest an extra generated/
    # subfolder inside GENERATED_DIR. So: drop any absolute anchor/drive,
    # and drop one leading "generated" path segment if present, before
    # resolving — since GENERATED_DIR already *is* that directory.
    parts = [p for p in Path(path).parts if p != Path(path).anchor]
    if parts and parts[0] == "generated":
        parts = parts[1:]

    candidate = base.joinpath(*parts).resolve() if parts else base
    if not candidate.is_relative_to(base):
        raise ValueError(f"path '{path}' escapes the generated/ directory")
    return candidate


@tool
def write_file(file_path: str, content: str) -> str:
    """Atomically create or overwrite a cocotb testbench (.py) file with complete content."""
    try:
        target = _resolve_safe_path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory first, then rename over
        # the real target. os.replace() is atomic on both POSIX and Windows,
        # so a crash or interrupted write can never leave a half-written or
        # corrupted .py file at the target path — the file either has its
        # old complete contents or its new complete contents, never a mix.
        tmp_path = target.parent / (target.name + ".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
        rel = target.relative_to(Path(GENERATED_DIR).resolve())
        return f"Wrote {len(content)} characters to {rel}"
    except (OSError, ValueError) as e:
        return f"Failed to write {file_path}: {e}"


@tool
def read_file(file_path: str) -> str:
    """Read the entire contents of a file so the agent can review an existing testbench."""
    try:
        target = _resolve_safe_path(file_path)
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"File not found: {file_path}"
    except (OSError, ValueError) as e:
        return f"Failed to read {file_path}: {e}"


@tool
def edit_file_block(file_path: str, target_string: str, replacement_string: str) -> str:
    """Replace one exact block of code in a file with new code, without rewriting the whole file — use for small, targeted fixes."""
    try:
        target = _resolve_safe_path(file_path)
        text = target.read_text(encoding="utf-8")
        count = text.count(target_string)
        # Require exactly one match: zero means nothing to edit, more than
        # one means target_string is ambiguous about which occurrence to
        # change — both are reported back instead of guessing.
        if count == 0:
            return f"target_string not found in {file_path} — no changes made."
        if count > 1:
            return (
                f"target_string appears {count} times in {file_path} — "
                "make it more specific so exactly one occurrence matches."
            )
        new_text = text.replace(target_string, replacement_string, 1)
        tmp_path = target.parent / (target.name + ".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        os.replace(tmp_path, target)
        return f"Replaced 1 occurrence in {file_path}."
    except FileNotFoundError:
        return f"File not found: {file_path}"
    except (OSError, ValueError) as e:
        return f"Failed to edit {file_path}: {e}"


@tool
def check_testbench(file_path: str) -> str:
    """Statically check a cocotb testbench (.py) file for syntax errors and likely bugs (undefined names, unused/missing imports, redefinitions) — no simulator or DUT involved, so this cannot confirm the testbench is functionally correct, only that it is not obviously broken Python."""
    try:
        target = _resolve_safe_path(file_path)
        if not target.exists():
            return f"File not found: {file_path}"

        text = target.read_text(encoding="utf-8")

        # compile() is a real, independent syntax check, not redundant with
        # ruff below — confirmed directly that ruff's --select F can miss a
        # genuine SyntaxError (e.g. `await` used inside a `def` that's
        # missing `async`, a real mistake seen in an actual generated
        # testbench: ruff reported "All checks passed!" on it). Note this
        # must be compile(), not ast.parse(): ast.parse only checks
        # grammar, but "await outside async function" is a semantic check
        # CPython performs during compilation, not parsing — confirmed
        # directly that ast.parse() does NOT raise on that exact case while
        # compile() does. compile() runs in-process (no subprocess, no temp
        # .pyc file — the returned code object is just discarded) and is
        # the authoritative check, being the same step CPython itself would
        # perform to run the file; ruff's F-rules are additionally useful
        # for the undefined-name/unused-import/redefinition checks it does
        # cover correctly, so both run rather than one replacing the other.
        try:
            compile(text, str(target), "exec")
        except SyntaxError as e:
            return f"Check failed: SyntaxError: {e.msg} (line {e.lineno}, column {e.offset})"

        # A file with zero @cocotb.test() functions or zero assert
        # statements passes both checks above cleanly but tests nothing —
        # the single most likely "model gave up / wrote a stub" failure
        # mode for this task, and otherwise invisible to either check.
        # Checked before the ruff subprocess call since it's a cheap,
        # decisive signal on its own — no point spending a subprocess call
        # on a file that's trivially not a real testbench yet.
        if "@cocotb.test" not in text:
            return "Check failed: no @cocotb.test() function found — file defines no actual test."
        if "assert" not in text:
            return "Check failed: no assert statement found — a test that never checks anything is not a valid testbench."

        # --select F restricts to ruff's pyflakes-reimplementation rules
        # (undefined names, unused/missing imports, redefinitions) —
        # ruff's default rule set is broader and would flag cosmetic things
        # like import-sort-order, which isn't a real bug and would just
        # distract the model from actual problems. Genuine SyntaxErrors are
        # still caught unconditionally: parse failures aren't a lint rule,
        # so --select doesn't suppress them. --isolated ignores any stray
        # ruff.toml/pyproject.toml on the host machine so the check is
        # fully deterministic regardless of environment — same sandboxing
        # discipline as _resolve_safe_path. sys.executable -m ruff (not a
        # bare `ruff` on PATH) since ruff is a pip dependency of this same
        # virtualenv, not a separate system install.
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "F", str(target)],
            capture_output=True, text=True, timeout=15,
        )
        log = (result.stdout + result.stderr).strip()

        if result.returncode == 0:
            return log or "Checked successfully — no errors."
        return f"Check failed (exit code {result.returncode}):\n{log}"

    except FileNotFoundError:
        # Raised if the ruff subprocess itself can't be executed at all
        # (not just a lint error in the .py file) — shouldn't happen since
        # ruff is a declared pyproject.toml dependency, but surfaced with
        # an actionable message rather than a raw traceback.
        return (
            "ruff is not installed or could not be found. Install it via "
            "`pip install ruff` (already a declared dependency of this project)."
        )
    except subprocess.TimeoutExpired:
        return f"Check of {file_path} timed out after 15s."
    except (OSError, ValueError) as e:
        return f"Failed to check {file_path}: {e}"


def _generate_stub_dut(declared: dict[str, tuple[str, int]]) -> str:
    # Builds a placeholder SystemVerilog module named TopModule with one
    # port per DECLARED signal (from _parse_declared_ports) — no internal
    # logic at all, since this exists purely so cocotb has something to
    # attach signal handles to.
    port_lines = []
    for name in sorted(declared):
        direction, width = declared[name]
        width_decl = "" if width == 1 else f"[{width - 1}:0] "
        port_lines.append(f"    {direction} logic {width_decl}{name}")

    ports = ",\n".join(port_lines)
    # `timescale is required — Icarus's default simulator precision (1s)
    # can't represent a typical ns-scale cocotb Clock period at all,
    # confirmed directly: without this, Clock(..., 10, unit="ns") fails
    # with "Unable to accurately represent 10(ns) with the simulator
    # precision of 1e0" before any real test logic even runs.
    return f"`timescale 1ns/1ps\nmodule TopModule(\n{ports}\n);\nendmodule\n"


@tool
def simulate_testbench(file_path: str) -> str:
    """Actually run a cocotb testbench (.py) against an auto-generated placeholder module with no real logic, using Icarus Verilog — this is a smoke test, not a correctness check: it exists to catch real runtime bugs a static check can't see (wrong/undeclared signal names, bad async/await usage, indexing errors, invalid values), not to confirm the design behaves correctly, since the placeholder module has no behavior of its own to compare against. The placeholder's ports come from the port-list comment you restate at the top of the file, not from scanning your own dut.<name> usage — so restate it accurately, since it's the only ground truth this tool has."""
    try:
        target = _resolve_safe_path(file_path)
        if not target.exists():
            return f"File not found: {file_path}"

        text = target.read_text(encoding="utf-8")
        if "@cocotb.test" not in text:
            return "Simulation failed: no @cocotb.test() function found — nothing to run."

        declared = _parse_declared_ports(text)
        if not declared:
            return (
                "Simulation failed: no parseable port-list comment found at the top of "
                "the file (expected a line per port like '- name (direction, N bits)'). "
                "Add one — it's what the placeholder module is built from."
            )

        # Checked before ever invoking the simulator: both are purely
        # textual cross-checks against the declared list, cheap and
        # deterministic, and catch exactly the two failure modes a live
        # simulator run can't distinguish from a real design bug (there is
        # no real design here to blame either way).
        referenced = set(_DUT_REF_RE.findall(text)) - _DUT_INTERNAL_ATTRS
        undeclared = sorted(referenced - declared.keys())
        if undeclared:
            return (
                f"Simulation failed: signal(s) {', '.join(undeclared)} are used in the "
                "testbench but not in the restated port-list comment — likely a typo of "
                "a declared port name, or a port missing from the list."
            )

        written = set(_DUT_WRITE_RE.findall(text))
        written_outputs = sorted(n for n in written if declared[n][0] == "output")
        if written_outputs:
            return (
                f"Simulation failed: signal(s) {', '.join(written_outputs)} are declared "
                "as output in the port list, but the testbench assigns a value to them — "
                "a testbench should only ever drive inputs and read outputs."
            )

        base = Path(GENERATED_DIR).resolve()
        stub_path = base / "_stub_dut.sv"
        stub_path.write_text(_generate_stub_dut(declared), encoding="utf-8")
        build_dir = base / "_sim_build"

        result = subprocess.run(
            [sys.executable, SIM_RUNNER_PATH, str(target), str(stub_path), str(build_dir)],
            capture_output=True, text=True, timeout=60,
        )
        log = (result.stdout + result.stderr).strip()

        if result.returncode == 0:
            return log or "Simulation succeeded — no errors."
        return f"Simulation failed:\n{log}"

    except subprocess.TimeoutExpired:
        # A testbench with no exit condition (e.g. an await loop waiting
        # for a value the placeholder module can never produce, since it
        # has no logic) would otherwise hang forever — this is the backstop.
        return f"Simulation of {file_path} timed out after 60s — check for an await/loop with no way to complete against a placeholder module."
    except (OSError, ValueError) as e:
        return f"Failed to simulate {file_path}: {e}"


@tool
def list_directory(path: str = ".") -> str:
    """List files and subdirectories at a path, to discover project structure and existing testbenches."""
    try:
        target = _resolve_safe_path(path)
        if not target.exists():
            return f"Path not found: {path}"
        if not target.is_dir():
            return f"Not a directory: {path}"
        entries = sorted(target.iterdir())
        if not entries:
            return f"(empty directory: {path})"
        return "\n".join(entry.name + ("/" if entry.is_dir() else "") for entry in entries)
    except (OSError, ValueError) as e:
        return f"Failed to list {path}: {e}"


# Tools available to the model. `@tool` turns each function into a
# StructuredTool object, not a plain function — it is NOT directly callable
# as write_file(**args) (that raises "object is not callable"). The correct
# call is TOOL_FUNCTIONS[name].invoke(args), passing the whole args dict
# rather than unpacking it as keyword arguments (see testbench_agent.py).
BASE_TOOLS = [write_file, read_file, edit_file_block, list_directory]

# check_testbench and simulate_testbench are not mutually exclusive
# backends the way TigressRTL's icarus/slang are — they're two stages of
# the same check, run in sequence: testbench_agent.py auto-runs
# check_testbench after every write/edit, and only if that's clean does it
# also auto-run simulate_testbench (no point spending 60s building and
# simulating Python that's already known to be broken). Both are still
# exposed as explicitly callable tools too, same as check_testbench alone
# was before, so the model can re-run either on its own if it wants to.
CHECK_TOOLS = [check_testbench, simulate_testbench]
