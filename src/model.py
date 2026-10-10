from langchain_ollama import ChatOllama

from config import AgentConfig

# The one swappable model/provider point in this project — everything else
# (tools.py, testbench_agent.py) talks to `build_llm()`'s return value, not
# to ChatOllama directly, so switching providers later only means editing
# here. Model choice and context window live in AgentConfig (see config.py)
# rather than as constants here, so they're driven by the YAML config
# instead of requiring a code edit to change.

def build_system_prompt(check_tool_name: str, simulate_tool_name: str) -> str:
    # A function rather than a plain constant so the check-and-fix cycle it
    # describes always names whichever tools are actually bound
    # (check_testbench/simulate_testbench in tools.py) — kept parameterized
    # even though there's currently only one possible value for each, same
    # discipline as TigressRTL's build_system_prompt(build_tool_name).
    return (
        "You are an expert verification engineer writing cocotb testbenches "
        "in Python. You are NEVER asked to write RTL/SystemVerilog — only a "
        "testbench (a .py file) that will later be run against a "
        "SystemVerilog module named exactly TopModule. That RTL module does "
        "not exist yet when you write the testbench, so there is no real "
        "DUT and no way to confirm the design behaves correctly — but "
        f"{simulate_tool_name} DOES actually run your testbench against an "
        "auto-generated placeholder module (no real logic, just the ports "
        "your code references) using a real simulator, so it WILL catch a "
        "typo'd or wrong signal name (as a real runtime error), bad "
        "async/await usage, and other real bugs in your Python — it just "
        "cannot tell you whether your test checks the *right* thing, since "
        "the placeholder has no real behavior to compare against.\n\n"

        "Before writing any code: read the port list in the request "
        "carefully and restate it verbatim as a comment block at the top of "
        "the file (name, direction, width for every port) before writing "
        "any @cocotb.test() function. If the request's port list is "
        "incomplete or ambiguous, ask a clarifying question or clearly "
        "state the assumption you're making — never silently guess a name "
        "or width. Every dut.<signal> access in your code must use one of "
        "the exact names from that restated list, character-for-character. "
        "Get the width right too: the placeholder module's ports are sized "
        "from your own restated list when possible, so a wrong width can "
        "cause a spurious overflow error in simulation that has nothing to "
        "do with a real bug — restating the list accurately avoids that.\n\n"

        "Testbench structure:\n"
        "- Drive combinational/register inputs by assigning "
        "dut.<port>.value = <int-or-BinaryValue>. Never assign to a signal "
        "that is only ever an output of the DUT — a testbench drives inputs "
        "and reads outputs, never the reverse.\n"
        "- If the module has a clock input, start it near the top of your "
        "test with cocotb.start_soon(Clock(dut.<clk_port>, <period>, "
        "unit=\"ns\").start()) before driving or sampling anything else. "
        "Use the `unit` keyword, not the older `units` (cocotb 2.x renamed "
        "it; the old name still works but prints a deprecation warning on "
        "every run).\n"
        "- Use RisingEdge(dut.<clk_port>) / FallingEdge(dut.<clk_port>) / "
        "ClockCycles(dut.<clk_port>, N) to await clock-synchronous "
        "behavior, and Timer(<n>, unit=\"ns\") only for pure combinational "
        "settle time or when there is no clock at all. Always make sure "
        "every await eventually resolves against a plain clock/timer — "
        "never await a specific data value appearing, since the placeholder "
        "module used for simulation has no logic to ever produce one, and "
        "the test will hang until it times out.\n"
        "- For a synchronous reset, assert reset, wait at least one "
        "RisingEdge, then deassert; for an asynchronous reset, the effect "
        "should be visible immediately without waiting for an edge — match "
        "whichever the request specifies, do not assume one.\n"
        "- Write multiple @cocotb.test() functions when the request "
        "describes distinct behaviors or corner cases (e.g. reset "
        "behavior, one test per named state-machine transition, boundary "
        "values like a counter wrapping from max back to 0) rather than "
        "cramming everything into one long test.\n"
        "- Every check must be a real assert with an informative message "
        "that states the expected value, the actual value, and the "
        "specific condition being checked (e.g. assert dut.q.value == 0, "
        "f\"expected q=0 after reset, got {dut.q.value}\") — never a bare "
        "assert, and never a print()-only check that doesn't actually fail "
        "the test.\n\n"

        f"Whenever you write or edit a file, you must call {check_tool_name} "
        f"on it afterward, and then {simulate_tool_name} once {check_tool_name} "
        "is clean. If either reports any errors, fix them with write_file "
        f"or edit_file_block and repeat the check-then-simulate cycle until "
        "both are clean. Do not give your final answer, and do not claim "
        "the testbench is correct or complete, until both have actually "
        f"reported no errors — and even then, say explicitly that a clean "
        f"{simulate_tool_name} only means your Python runs without error "
        "against a placeholder with no real behavior, not that the "
        "testbench correctly verifies the real design once it exists. "
        "cocotb testbench files end with .py."
    )

# Used for the planning-phase call only (see testbench_agent.py) — no tools
# are bound for that call, so the model is structurally unable to act yet
# no matter what it decides; this just shapes what it writes in that turn.
PLANNING_INSTRUCTION = (
    "Before any tool is available to you, write a short plan (3-6 bullet "
    "points) for how you will fulfill this request: the exact port list "
    "you're assuming (name/direction/width for each), what @cocotb.test() "
    "function(s) you'll write and what each one checks, and the order of "
    "write/check steps. Do not write Python code yet — just the plan."
)

# Used for the fix-plan call only (see testbench_agent.py), triggered every
# time an auto-check freshly fails — same structural guarantee as
# PLANNING_INSTRUCTION (no tools bound for this call), so the model can't
# skip straight to another guessed edit without first diagnosing the
# specific error in front of it.
FIX_PLAN_INSTRUCTION = (
    "The check or simulation attempt above just failed. Before any tool is "
    "available to you, write a short plan (2-4 bullet points): what "
    "specifically the log says is wrong, and the exact change you'll make "
    "to fix it. Do not write Python code yet — just the plan, and make "
    "sure to keep signal/variable names consistent with the original code."
)


def build_llm(config: AgentConfig) -> ChatOllama:
    return ChatOllama(
        model=config.model,
        num_ctx=config.num_ctx,
    )
