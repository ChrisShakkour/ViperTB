# Config options

`configs/default.yaml` (or any file passed via `--config`) accepts the
fields defined by `AgentConfig` in `src/config.py`:

| Field | Default | What it controls |
|---|---|---|
| `model` | `devstral` | Which pulled Ollama model to use. |
| `num_ctx` | `8192` | Context window size, in tokens, given to Ollama. Can be set anywhere up to the chosen model's maximum context length — larger values use more RAM/VRAM and run slower. May need to be sized up from this starting point once real generated testbenches (restated port list plus multiple `@cocotb.test()` functions) show how long they typically run — this default is carried over from TigressRTL's RTL-generation task, not independently re-tuned for this one yet. |
| `max_check_retries` | `3` | How many times the agent is forced to retry after a check failure it didn't actually fix, before giving up for that turn. Any non-negative integer. A static Python check is a much lower bar to satisfy than a real compile, so this may turn out generous — also carried over from TigressRTL's tuned-for-RTL default, not independently re-tuned yet. |

There is no `verilog_build_tool`-equivalent field — unlike TigressRTL, which
switches between two mutually exclusive build/lint backends, this agent has
exactly one check path (`check_testbench` in `src/tools.py`, backed by
`ruff`), so there's nothing to select between.

To run an experiment with different settings, copy `default.yaml`, edit the
copy, and pass it via `--config` (e.g. `--config configs/my-experiment.yaml`).
`--model` always overrides just the model field for a one-off run without
needing a new file. A typo'd key in your YAML raises a clear error instead
of silently falling back to the wrong default — see `load_config()` in
`../src/config.py`.

## Models

Only one model has been empirically verified against *this* agent's specific
tool set (`write_file`/`read_file`/`edit_file_block`/`list_directory`/
`check_testbench`) and prompt shape — tool-calling reliability is model- and
task-specific, not something that automatically carries over from a
different agent even if the underlying model is the same one TigressRTL
verified:

| Model | Verified here? | Notes |
|---|---|---|
| `devstral-small-2` | Yes | Confirmed via a real interactive run: restated the exact port list before writing code, produced a genuinely structured testbench (multiple `@cocotb.test()` functions, informative asserts), reliably called `check_testbench` and reacted to its output. Also the model TigressRTL's own `configs/README.md` recommends for RTL generation, but that recommendation is about a different tool set — re-verified independently for this task rather than assumed. |
| `devstral`, `llama3.2`, `qwen2.5-coder`, `llama2` | Not yet | TigressRTL's `configs/README.md` documents these for the RTL-generation tool set specifically — don't assume the same reliability/unreliability carries over here without checking. |

Whatever model you choose, `num_ctx` in your config must not exceed its
maximum context length — Ollama will error or silently clamp it otherwise.
See TigressRTL's `configs/README.md` for a maximum-context-length table per
model (that information is model-specific, not agent-specific, so it does
transfer).
