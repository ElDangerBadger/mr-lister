"""Executable entrypoint for the dedicated Phase 6 AgentCore Strands runtime.

This is intentionally separate from ``agentcore_runtime.py``, which remains the disposable
Phase 3 synthetic canary.  The release bundle copies this file as ``main.py``.
"""

from __future__ import annotations

import os
import sys

# Preserve the sealed source-byte authority even when the container workdir is writable.
sys.dont_write_bytecode = True

from mr_lister.release.phase6 import verify_phase6_packaged_release  # noqa: E402

environment: dict[str, object] = dict(os.environ)
verify_phase6_packaged_release(environment, component="agentcore")

from mr_lister.agent.phase6_composition import build_phase6_agentcore_runtime  # noqa: E402

app = build_phase6_agentcore_runtime(environment)

if __name__ == "__main__":
    app.run(host=os.getenv("MR_LISTER_AGENTCORE_HOST", "0.0.0.0"), port=8080)
