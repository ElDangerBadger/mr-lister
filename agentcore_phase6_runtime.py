"""Executable entrypoint for the dedicated Phase 6 AgentCore Strands runtime.

This is intentionally separate from ``agentcore_runtime.py``, which remains the disposable
Phase 3 synthetic canary.  The release bundle copies this file as ``main.py``.
"""

from __future__ import annotations

import os

from mr_lister.agent.phase6_composition import build_phase6_agentcore_runtime

app = build_phase6_agentcore_runtime(dict(os.environ))

if __name__ == "__main__":
    app.run(host=os.getenv("MR_LISTER_AGENTCORE_HOST", "0.0.0.0"), port=8080)
