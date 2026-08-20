from __future__ import annotations

import subprocess
import sys


def test_agent_contracts_do_not_require_optional_strands_runtime() -> None:
    script = """
import builtins

original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "strands" or name.startswith("strands."):
        raise AssertionError("contract import attempted to load Strands")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from mr_lister.agent.contracts import AgentCoreResponse
assert AgentCoreResponse.__name__ == "AgentCoreResponse"
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
