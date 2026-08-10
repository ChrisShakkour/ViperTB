from dataclasses import dataclass
from pathlib import Path

import yaml

# Repo root, not src/ — configs/ sits alongside src/, not inside it.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "default.yaml"


@dataclass
class AgentConfig:
    model: str = "devstral"

    # Sized up from a typical 4096 default, same rationale as TigressRTL —
    # may need independent re-tuning once real generated testbenches show
    # how long a restated-port-list-plus-multiple-@cocotb.test() file
    # actually runs; not assumed to carry over from the RTL agent unchecked.
    num_ctx: int = 8192

    # Renamed from TigressRTL's max_build_retries — there's no build step
    # here, just a static check (check_testbench in tools.py), so "check"
    # is the accurate word. Bounded for the same reason: without a cap, a
    # model that can never satisfy the check would loop forever.
    max_check_retries: int = 3

    # No build-tool-selection field (unlike TigressRTL's verilog_build_tool)
    # on purpose. There is exactly one check path here (ruff via
    # check_testbench), not two mutually exclusive backends — adding a
    # field for a hypothetical second backend now would be speculative.
    # TigressRTL added verilog_build_tool in its own later, dedicated
    # commit once a second backend actually existed; follow the same
    # discipline here rather than pre-guessing.

    def __post_init__(self):
        if self.max_check_retries < 0:
            raise ValueError(
                f"max_check_retries must be >= 0, got {self.max_check_retries}"
            )


def load_config(path: str | Path | None) -> AgentConfig:
    # `path=None` means "use the built-in defaults" — kept as a parameter
    # (rather than always reading DEFAULT_CONFIG_PATH directly) so callers
    # can skip the config file entirely.
    if path is None:
        return AgentConfig()

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    try:
        return AgentConfig(**data)
    except TypeError as e:
        # Passing an unknown key raises TypeError from the dataclass
        # constructor itself — a typo'd key (e.g. "num_ctx_" instead of
        # "num_ctx") fails loudly here instead of silently falling back to
        # a default the user didn't intend.
        raise ValueError(f"Invalid config file {path}: {e}") from e
