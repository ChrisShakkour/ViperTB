# ViperTB

An agent that writes cocotb testbenches in Python for SystemVerilog RTL
modules that don't exist yet. It runs a ReAct-style loop against a local LLM
(via [Ollama](https://ollama.com)) with tools to write, read, and edit
files, and to statically check them with [ruff](https://astral.sh/ruff) —
so generated testbenches are checked for real Python bugs, not just
eyeballed. This is a sibling project to
[TigressRTL](https://github.com/ChrisShakkour/TigressRTL) (which generates
the RTL itself); this repo deliberately never writes RTL.

**Read this before extending the project:** there is no simulator, no DUT,
and no reference RTL anywhere in this agent's loop. It cannot confirm a
testbench is functionally correct — only that it's well-formed Python. See
[Design notes](#design-notes) for why, and what that means in practice.

## Requirements

- **Python 3.10+**
- **Ollama**, running locally, with a tool-capable model pulled — see
  [Installing Ollama](#installing-ollama) below.
- No simulator, compiler, or HDL toolchain is required — `check_testbench`
  (the agent's one validation tool) is pure Python (`ruff`, a `pip`
  dependency installed with the rest of this project), not a system-level
  install.

A Python virtual environment (below) is the only setup needed beyond
Ollama itself.

## Setup

Works the same whether you're setting this up locally or on a freshly
cloned remote server:

```bash
git clone <this-repo-url>
cd ViperTB

python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install --upgrade pip
pip install -e .
```

## Installing Ollama

The agent talks to a local Ollama server for all model inference — install
it before running the agent.

**Windows:** download and run the installer from
[ollama.com/download/windows](https://ollama.com/download/windows) (or
`winget install Ollama.Ollama`). It puts `ollama` on `PATH` and runs as a
background service automatically — no separate `ollama serve` step needed.

**Linux:**

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

This is the official install script — it installs the `ollama` binary and
sets it up as a `systemd` service that starts automatically.

### Pulling a model

Once Ollama is installed and running, pull a tool-capable model:

```bash
ollama pull devstral-small-2   # default in configs/default.yaml — verified for this task, see configs/README.md
```

Run `ollama list` to confirm what's pulled locally. Only `devstral-small-2`
has been empirically verified against this agent's specific tool set and
prompt shape — see [configs/README.md](configs/README.md#models) before
assuming any other model (including ones TigressRTL verified for RTL
generation) behaves the same way here.

## Run

```bash
python src/testbench_agent.py                                    # uses configs/default.yaml
python src/testbench_agent.py --config configs/my-experiment.yaml # a different config file
python src/testbench_agent.py --model llama3.2                    # override just the model for one run
```

Settings (model, context window, check-retry cap) are loaded from a YAML
config file — `configs/default.yaml` unless `--config` points elsewhere.
`--model` always wins over whatever the config says, so a one-off override
doesn't require creating a new file. See [Config options](#config-options)
below for the full list of fields.

Type a request — a verilog-eval-style module spec works directly, e.g. the
contents of a `*_prompt.txt` file (see [Benchmarking](#benchmarking)) — and
the agent will plan, write a testbench file under `src/generated/`, and
check it automatically. Type `exit` or `quit` to stop.

While it runs, several prefixes tell you what's actually happening:

- `[Plan]` — the model's stated plan, produced before any tool is available
  to it, including the exact port list it's assuming.
- `[Action]` / `[Result]` — a tool call and its real return value.
- `[Auto-Check]` — the static-check result, run automatically after every
  write/edit.
- `[Fix Plan]` — a forced diagnosis-and-plan step, triggered every time a
  check freshly fails, before the model is allowed to attempt another edit.

Always trust `[Result]`/`[Auto-Check]` over the `Assistant:` text that
follows — see [Design notes](#design-notes) below. And remember: even a
completely clean `[Auto-Check]` only means the file is valid Python that
defines a real test — it says nothing about whether the testbench actually
drives or checks the right signals.

## Config options

Settings (model, context window, check-retry cap) and the full list of
fields are documented in [`configs/README.md`](configs/README.md), along
with what's actually been empirically verified for this task versus
carried over unchecked from TigressRTL.

## Benchmarking

Two scripts, kept separate since generation and validation are different
concerns run at different times: `test/run_benchmark.py` runs the agent
against the spec-to-rtl problems from
[NVlabs/verilog-eval](https://github.com/NVlabs/verilog-eval) — using only
their `*_prompt.txt` module specs, never their `_ref.sv`/`_test.sv` — and
saves whatever testbench it produced; `test/run_validation.py` takes that
output and independently re-checks it.

Clone the dataset anywhere (it's not vendored into this repo, same as
Ollama):

```bash
git clone https://github.com/NVlabs/verilog-eval.git
```

Run it:

```bash
# One config, all 156 problems
python test/run_benchmark.py --dataset-dir verilog-eval/dataset_spec-to-rtl --configs configs/default.yaml

# Quick smoke test — first 2 problems only
python test/run_benchmark.py --dataset-dir verilog-eval/dataset_spec-to-rtl --configs configs/default.yaml --limit 2
```

Each invocation runs every problem as a **fresh `testbench_agent.py`
subprocess** (its own context, no history carried over between problems) and
writes into a new timestamped directory under `benchmark_runs/` (gitignored):

```
benchmark_runs/2026-08-10_22-35-16/
  command.txt              # the exact CLI invocation, for reproducibility
  configs/default.yaml      # a copy of every --configs file used in this run
  default/                  # config-stem = Path(config).stem
    Prob001_zero/
      prompt.txt            # the single-lined prompt piped into testbench_agent.py
      transcript.log        # full agent output ([Plan]/[Action]/[Result]/[Auto-Check]/[Fix Plan]/Assistant)
      status.json           # {problem, model, num_ctx, status, py_file_count, duration_s,
                             #  input_tokens, output_tokens, total_tokens, ...}
      generated/             # whatever ended up in src/generated/ after this run
```

If a long sweep gets interrupted, resume it with
`--resume-dir benchmark_runs/<timestamp>`, which skips any (config, problem)
pair already marked `"completed"` and retries anything that timed out or
errored. See `--help` for `--problems` (filter by name/glob), `--limit`,
`--timeout`, and `--python`.

`testbench_agent.py` prints a `[Token Usage]` line on exit, parsed into each
problem's `status.json` and summed into the end-of-run summary, same
mechanism as TigressRTL's `rtl_agent.py`.

### Validating generated code

`test/run_validation.py` takes a `run_benchmark.py` output directory and,
for every generated `.py` file, independently re-checks it (the same three
checks `check_testbench` does — a real `compile()` syntax check, a
`@cocotb.test()`/`assert` presence check, and `ruff --select F` — kept as
an independent re-implementation, not a call into the agent's own code) and
heuristically cross-checks every `dut.<signal>` reference against the port
list declared in that problem's own `*_prompt.txt`:

```bash
python test/run_validation.py --run-dir benchmark_runs/2026-08-10_22-35-16 \
    --dataset-dir verilog-eval/dataset_spec-to-rtl
```

Writes `validation.json` (`status`, `duration_s`, and either `errors` or
`suspected_signals`/`declared_ports` depending on status) alongside each
problem's existing `transcript.log`. `status` is one of: `clean`,
`static_errors` (a real problem: syntax error, undefined name, no
`@cocotb.test()`, or no `assert`), `no_generated_code`,
`port_mismatch_suspected` (references a `dut.<signal>` not in the declared
port list — likely, not certain, a typo or hallucinated name), or
`unparseable_ports`. Already-validated problems are skipped on a re-run
unless `--overwrite` is passed.

At the end, a categorized summary is both printed and written to
`<run-dir>/validation_summary.json` — it reflects the true current state of
the whole run directory (including already-validated, skipped problems),
not just what changed in that particular invocation.

**⚠️ Unlike TigressRTL's `run_validation.py`, this is NOT a correctness
signal.** There is no reference RTL to run the testbench against — the
whole point of this agent is writing testbenches for modules that don't
exist yet, so there's nothing to simulate against and no way to confirm the
testbench actually catches a bug. `clean` only re-confirms Python
well-formedness independently of the (possibly stale) transcript.
`port_mismatch_suspected` is a heuristic, not a proof. Confirmed directly,
though: keeping this as an independent re-check still catches real
problems the live transcript missed — in testing, two generated testbenches
both had a genuine `SyntaxError` (`await` used in a `def` missing `async`)
that an earlier version of `check_testbench` had wrongly reported as clean
(a bug in the check tool itself, since fixed — see [Design
notes](#design-notes)); their transcripts still say "All checks passed!",
but re-running `run_validation.py` correctly flags both as `static_errors`.

## Architecture

- **`src/config.py`** / **`configs/default.yaml`** — `AgentConfig`, a small
  dataclass (`model`, `num_ctx`, `max_check_retries`) loaded from YAML via
  `load_config()`. No build-tool-selection field, unlike TigressRTL — there
  is only one check path here.
- **`src/model.py`** — the one swappable model/provider point (`build_llm()`
  takes an `AgentConfig`), plus `build_system_prompt(check_tool_name)` with
  cocotb-specific guidance (driving `dut.<port>.value`, clock/reset
  handling, multiple `@cocotb.test()` functions, informative asserts) and
  an explicit statement of what the check tool can't verify.
- **`src/tools.py`** — `BASE_TOOLS` (`write_file`, `read_file`,
  `edit_file_block`, `list_directory` — sandboxed to `src/generated/`,
  same path-escape protection as TigressRTL) plus `check_testbench`: a
  `compile()` syntax check, an empty-testbench heuristic
  (`@cocotb.test()`/`assert` presence), and `ruff --select F`, in that
  order. No multi-file-compile-together concept — a single `.py` file has
  no cross-file linkage requirement for a lint check.
- **`src/testbench_agent.py`** — the interactive loop: a planning call with
  no tools bound (so the model is structurally unable to act before
  planning), then a ReAct tool-calling loop that auto-runs
  `check_testbench` after every write/edit, forces a tools-unbound
  "diagnose and plan the fix" call every time a check freshly fails (before
  the model is allowed to attempt another edit), and caps retries at
  `config.max_check_retries`.
- **`test/run_benchmark.py`** — sweeps `testbench_agent.py` (one fresh
  subprocess per problem) over verilog-eval's problem specs — see
  [Benchmarking](#benchmarking) above.
- **`test/run_validation.py`** — independently re-checks each generated
  testbench and heuristically flags likely port-name mismatches, recording
  a necessarily weaker signal than TigressRTL's real pass/fail — see
  [Validating generated code](#validating-generated-code) above.

## Design notes

A few non-obvious things learned building this, worth knowing before
extending it:

- **This agent has no ground truth, by design, not by oversight.** The RTL
  module a testbench targets doesn't exist yet when the testbench is
  written — there's no simulator, no DUT, no reference implementation
  anywhere in this loop. That's a genuine, accepted limitation: a testbench
  that never asserts anything would pass every check here just as cleanly
  as a rigorous one. `check_testbench` mitigates the most likely version of
  that failure mode directly (it requires at least one `@cocotb.test()` and
  one `assert` to even be considered), but that's a floor, not a
  correctness guarantee.
- **A wrong `dut.<signal>` name is invisible to every static tool here.**
  `dut.<signal>` is a dynamic attribute access resolved only at real
  simulation time against a real DUT — no Python linter, and no simulator
  that doesn't exist in this project, can catch a typo'd or hallucinated
  port name. The system prompt's insistence on restating the exact port
  list before writing code, and `run_validation.py`'s
  `port_mismatch_suspected` heuristic (cross-checking against the prompt's
  own declared ports), are both mitigations for this specific blind spot,
  not fixes for it.
- **`ast.parse()` is not a complete syntax check — `compile()` is.**
  Discovered directly, not assumed: a real generated testbench had
  `@cocotb.test()` on a `def` missing `async`, with `await` used inside it
  — a genuine `SyntaxError` ("'await' outside async function"). `ruff
  check --select F` reported it as clean. `ast.parse()` on the same code
  also does not raise, because that specific error is a semantic check
  CPython performs during compilation, not a grammar-level parse error.
  Only `compile(text, filename, "exec")` catches it — confirmed both
  functions' behavior directly against the identical broken code before
  settling on `compile()` in `check_testbench`. This is now the first
  check `check_testbench` runs, before the ruff subprocess call.
- **Never trust the model's prose over a tool's actual return value** —
  same discipline as TigressRTL: every tool result is printed
  unconditionally via `[Result]`/`[Auto-Check]`, before the model gets a
  chance to say anything about it.
- **A prompt instruction to "plan before acting" isn't reliable enough on
  its own** — same structural-guarantee pattern as TigressRTL:
  `testbench_agent.py` makes tools-unbound calls for both the initial plan
  and every post-failure fix plan, so the model is structurally incapable
  of acting before articulating one, not just asked to.
- **The raw verilog-eval prompt wording says "implement a module," not
  "write a testbench" — and the model handles that correctly anyway.**
  Confirmed directly in testing: despite every `*_prompt.txt` being written
  as an RTL-implementation request, the system prompt's explicit "you are
  NEVER asked to write RTL" framing reliably overrode the literal wording
  in the one human turn, and the model went straight to writing a cocotb
  testbench without attempting SystemVerilog. Worth re-confirming if the
  prompt or model changes, since this isn't a structural guarantee the way
  the planning-call mechanism is — it's an empirical behavior.
