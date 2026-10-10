import argparse
import dataclasses
import sys

from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

from config import DEFAULT_CONFIG_PATH, load_config
from model import build_llm, build_system_prompt, FIX_PLAN_INSTRUCTION, PLANNING_INSTRUCTION
from tools import BASE_TOOLS, check_testbench, simulate_testbench

# Windows terminals default to a codepage that can't render some characters —
# force UTF-8 output so nothing gets garbled.
sys.stdout.reconfigure(encoding="utf-8")

parser = argparse.ArgumentParser(description="Chat with a cocotb testbench-writing assistant.")
parser.add_argument(
    "--config", default=str(DEFAULT_CONFIG_PATH),
    help="Path to a YAML config file (see configs/default.yaml)",
)
parser.add_argument(
    "--model", default=None,
    help="Override the model from the config, e.g. llama3.2, qwen2.5-coder "
         "(must already be pulled via `ollama pull`)",
)
args = parser.parse_args()


def accumulate_tokens(totals: dict, response) -> None:
    # ChatOllama populates AIMessage.usage_metadata on every non-streaming
    # .invoke() call — the `or {}`/.get(..., 0) guards are defensive only,
    # not expected to actually trigger with this provider.
    usage = getattr(response, "usage_metadata", None) or {}
    totals["input_tokens"] += usage.get("input_tokens", 0)
    totals["output_tokens"] += usage.get("output_tokens", 0)


# One agent process handles exactly one problem in the benchmark harness
# (test/run_benchmark.py runs a fresh subprocess per problem), so a
# process-lifetime total is already a per-problem total — no extra
# scoping needed.
token_totals = {"input_tokens": 0, "output_tokens": 0}

config = load_config(args.config)
if args.model:
    # A one-off override shouldn't require writing a new YAML file — this
    # takes precedence over whatever the config file says.
    config = dataclasses.replace(config, model=args.model)

# Unlike TigressRTL's config-driven build-tool selection (icarus vs slang),
# there's no selection here — both check tools always run, in sequence
# (static check first, then simulation only if that's clean; see the
# auto-check block below).
CHECK_TOOL = check_testbench
CHECK_TOOL_NAME = CHECK_TOOL.name
CHECK_FAILURE_PREFIX = "Check failed"
SIMULATE_TOOL = simulate_testbench
SIMULATE_TOOL_NAME = SIMULATE_TOOL.name
SIMULATE_FAILURE_PREFIX = "Simulation failed"
TOOLS = BASE_TOOLS + [CHECK_TOOL, SIMULATE_TOOL]
TOOL_FUNCTIONS = {t.name: t for t in TOOLS}

# `llm` is our handle to the model. `.bind_tools(TOOLS)` returns a new
# runnable that knows about each tool's schema and may respond with
# tool_calls instead of (or alongside) plain text — the model itself never
# executes anything, it only ever *requests* a call.
llm = build_llm(config)
llm_with_tools = llm.bind_tools(TOOLS)

# `messages` is the full conversation history sent on every request —
# the API is stateless, so the whole transcript is resent each time. Each
# turn is a typed message object:
#   SystemMessage(...) - instructions, sent once up front
#   HumanMessage(...)  - you
#   AIMessage(...)     - the model's turn (may carry .tool_calls)
#   ToolMessage(...)   - a tool's result, tagged with which call it answers
messages = [SystemMessage(content=build_system_prompt(CHECK_TOOL_NAME, SIMULATE_TOOL_NAME))]

print("Chatting with", config.model, "— a cocotb testbench-writing assistant.")
print("Type 'exit' or 'quit' to stop.\n")

while True:
    user_input = input("You: ").strip()
    if user_input.lower() in {"exit", "quit"}:
        print(
            f"[Token Usage] input_tokens={token_totals['input_tokens']} "
            f"output_tokens={token_totals['output_tokens']} "
            f"total_tokens={token_totals['input_tokens'] + token_totals['output_tokens']}"
        )
        break
    if not user_input:
        continue

    messages.append(HumanMessage(content=user_input))

    # Planning phase: call the PLAIN `llm` — no tools bound — so the model
    # is structurally incapable of acting yet, no matter what it decides.
    # This is a hard guarantee from the code, not a hope that a "plan
    # before acting" prompt instruction gets followed — local models have
    # repeatedly been observed skipping or misordering such instructions.
    plan_response = llm.invoke(messages + [HumanMessage(content=PLANNING_INSTRUCTION)])
    accumulate_tokens(token_totals, plan_response)
    print("[Plan]", plan_response.content, "\n")
    messages.append(plan_response)

    # Without this, `messages` would end in two consecutive assistant turns
    # (the plan, then immediately another assistant response with no new
    # user turn in between) once the execution loop below calls the model
    # again — an unusual pattern outside normal alternating user/assistant
    # chat structure that some models handle poorly (observed: an entirely
    # empty response with zero tool_calls). A short synthetic user turn
    # restores normal alternation and gives an explicit cue to switch from
    # planning to acting.
    messages.append(HumanMessage(content="Proceed with your plan now, using the available tools."))

    # Tracks whether the most recent auto-check (below) failed and hasn't
    # been fixed yet, and how many times we've already nudged for a fix
    # this turn — reset per user turn. See the retry check further down.
    check_failed = False
    check_retry_count = 0

    # Set whenever an auto-check freshly fails (below), so the very next
    # model turn gets a short, tools-unbound "diagnose and plan the fix"
    # call first — same structural-guarantee pattern as the initial
    # PLANNING_INSTRUCTION (a plain llm.invoke() with no tools bound, not a
    # prompt hope), since local models have been observed diving straight
    # into another guessed edit without pausing to actually read the error.
    # Cleared once that plan call has been made, so a retry nudge (model
    # skipped acting, not a fresh failure) doesn't trigger a second plan
    # for the same error.
    fix_plan_pending = False

    # Inner loop: the ReAct cycle for this one turn — keep calling the
    # model and executing whatever tools it requests until a response has
    # no more tool_calls, which is the model's final answer for this turn.
    while True:
        if fix_plan_pending:
            fix_plan_response = llm.invoke(messages + [HumanMessage(content=FIX_PLAN_INSTRUCTION)])
            accumulate_tokens(token_totals, fix_plan_response)
            print("[Fix Plan]", fix_plan_response.content, "\n")
            messages.append(fix_plan_response)
            messages.append(HumanMessage(content="Now apply that fix using the available tools."))
            fix_plan_pending = False

        try:
            response = llm_with_tools.invoke(messages)
        except Exception as e:
            # Some models have no tool-calling support in Ollama at all and
            # reject any request with tools bound. That 400 comes back as a
            # raw ResponseError traceback by default; recognize it and fail
            # with an actionable message instead, since every subsequent
            # turn would hit the same wall.
            if "does not support tools" in str(e):
                print(
                    f"\nError: model '{config.model}' does not "
                    "support tool calling in Ollama.\nThis agent's tools (write_file, "
                    f"read_file, edit_file_block, list_directory, {CHECK_TOOL_NAME}, "
                    f"{SIMULATE_TOOL_NAME}) require a tool-capable model.\nTry "
                    "--model llama3.2 instead — see README.md for what's been tested."
                )
                sys.exit(1)
            raise
        accumulate_tokens(token_totals, response)
        messages.append(response)

        # `response.tool_calls` is a list of dicts:
        #   {"name": "write_file", "args": {...}, "id": "..."}
        if not response.tool_calls:
            # A response with no tool_calls is only treated as the real
            # final answer if the last known check actually succeeded (or
            # never ran). A model can correctly diagnose a check failure in
            # prose, say it will fix it, and then simply not call
            # write_file/edit_file_block in that same response — auto-check
            # enforces that a failure is *seen*, but nothing forces it to be
            # *acted on*. So otherwise, nudge for a genuine fix attempt and
            # keep the loop going, up to config.max_check_retries times.
            if check_failed and check_retry_count < config.max_check_retries:
                check_retry_count += 1
                print(f"[Retry] Check is still failing and no fix was applied — "
                      f"forcing attempt {check_retry_count}/{config.max_check_retries}.")
                messages.append(HumanMessage(content=(
                    f"The last {CHECK_TOOL_NAME}/{SIMULATE_TOOL_NAME} result was a "
                    "failure, and you did not call write_file or edit_file_block to "
                    "actually apply a fix — you only described one. Call the "
                    "appropriate tool now with the corrected content."
                )))
                continue
            break  # model gave its final answer for this turn — inner loop ends

        for call in response.tool_calls:
            name = call["name"]
            print("[Action]", name, call["args"])

            # TOOL_FUNCTIONS[name] is a StructuredTool object (from @tool),
            # not a plain function — it must be called via .invoke(args),
            # passing the whole args dict, not unpacked as **args.
            tool_fn = TOOL_FUNCTIONS[name]
            result = tool_fn.invoke(call["args"])

            # Enforcement, not a request: the system prompt asks the model to
            # always check (then simulate) after writing, but that isn't
            # reliable — models have skipped the write entirely, called a
            # check tool on a file that doesn't exist yet, and narrated fake
            # tool calls as plain text instead of actually invoking them. So
            # instead of trusting the model to remember, the harness runs
            # both checks itself right after any successful write/edit, and
            # folds the results into the SAME tool response — the model
            # sees them whether or not it asked. simulate_testbench only
            # runs if check_testbench was clean: there's no point spending
            # ~60s building and simulating Python already known to be broken.
            if name in ("write_file", "edit_file_block") and result.startswith(("Wrote", "Replaced")):
                file_path = call["args"].get("file_path")
                check_result = CHECK_TOOL.invoke({"file_path": file_path})
                print("[Auto-Check]", check_result)
                static_failed = check_result.startswith(CHECK_FAILURE_PREFIX)

                auto_log = f"[automatically ran {CHECK_TOOL_NAME} after {name}]\n{check_result}"
                sim_failed = False
                if not static_failed:
                    sim_result = SIMULATE_TOOL.invoke({"file_path": file_path})
                    print("[Auto-Simulate]", sim_result)
                    sim_failed = sim_result.startswith(SIMULATE_FAILURE_PREFIX)
                    auto_log += f"\n\n[automatically ran {SIMULATE_TOOL_NAME} after {CHECK_TOOL_NAME} passed]\n{sim_result}"

                # Drives the retry check above: a real, current failure
                # (from either stage) sets this True; both stages clean
                # clears it (and resets the retry count) so a later,
                # unrelated failure gets its own fresh set of attempts
                # rather than inheriting an old count.
                check_failed = static_failed or sim_failed
                if not check_failed:
                    check_retry_count = 0
                else:
                    fix_plan_pending = True
                result = f"{result}\n\n{auto_log}"

            # Print the tool's actual return value directly — never rely on
            # the model's own later paraphrase of it. Models have been
            # observed confidently misreporting a tool result (e.g.
            # describing a full file when read_file actually returned a
            # single "{"), so this is the ground truth, shown regardless of
            # what the model goes on to say.
            print("[Result]", result)

            # ToolMessage links the result back to the specific call it
            # answers via tool_call_id — the model matches these up itself.
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))

    final = messages[-1]
    print("Assistant:", final.content or "(no final text — see actions above)", "\n")
