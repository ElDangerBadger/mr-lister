"""Executable Phase 3 AgentCore Runtime canary entrypoint."""

import os

from mr_lister.agent.agentcore_sdk import build_synthetic_canary_runtime

app = build_synthetic_canary_runtime()

if __name__ == "__main__":
    app.run(host=os.getenv("MR_LISTER_AGENTCORE_HOST", "0.0.0.0"), port=8080)
