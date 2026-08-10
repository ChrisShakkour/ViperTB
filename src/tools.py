import os
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

# Where the agent's testbench project lives. Kept separate from the
# script's own directory so it can be freely cleared/gitignored without
# touching the actual agent code.
GENERATED_DIR = os.path.join(os.path.dirname(__file__), "generated")


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

        # A file with zero @cocotb.test() functions or zero assert
        # statements passes ruff cleanly but tests nothing — the single
        # most likely "model gave up / wrote a stub" failure mode for this
        # task, and otherwise invisible to a pure lint check. Checked
        # before the lint call since it's a cheap, decisive signal on its
        # own — no point spending a subprocess call on a file that's
        # trivially not a real testbench yet.
        text = target.read_text(encoding="utf-8")
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

# No BUILD_TOOL_FUNCTIONS/BUILD_FAILURE_PREFIXES dicts, unlike TigressRTL —
# those existed there to abstract over which of two mutually exclusive
# backends (icarus/slang) is active. There's only one check tool here, so
# testbench_agent.py references check_testbench and its failure prefix
# ("Check failed") directly instead of through a single-key dict.
