#!/usr/bin/env python3
"""Prepare one sanitized Phase 6.6 provider-canary evidence fragment offline."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from tools.phase66_provider_acceptance import (
    ProviderAcceptanceError,
    _canonical,
    prepare_phase66_provider_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate-id",
        required=True,
        choices=(
            "provider.primary_same_job_canary",
            "provider.concurrency_canary",
            "provider.cancellation_canary",
        ),
    )
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--deployment-authority-sha256", required=True)
    parser.add_argument("--agentcore-authority", required=True, type=Path)
    parser.add_argument("--agentcore-authority-sha256", required=True)
    parser.add_argument("--prerequisite-records", required=True, type=Path)
    parser.add_argument("--prerequisite-records-sha256", required=True)
    parser.add_argument("--run-gate", required=True, type=Path)
    parser.add_argument("--run-gate-sha256", required=True)
    parser.add_argument("--provider-write-gate", required=True, type=Path)
    parser.add_argument("--provider-write-gate-sha256", required=True)
    parser.add_argument("--observation", required=True, type=Path)
    parser.add_argument("--observation-sha256", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        summary = prepare_phase66_provider_evidence(
            gate_id=arguments.gate_id,
            deployment_authority_path=arguments.deployment_authority,
            deployment_authority_sha256=arguments.deployment_authority_sha256,
            agentcore_authority_path=arguments.agentcore_authority,
            agentcore_authority_sha256=arguments.agentcore_authority_sha256,
            prerequisite_records_path=arguments.prerequisite_records,
            prerequisite_records_sha256=arguments.prerequisite_records_sha256,
            run_gate_path=arguments.run_gate,
            run_gate_sha256=arguments.run_gate_sha256,
            provider_write_gate_path=arguments.provider_write_gate,
            provider_write_gate_sha256=arguments.provider_write_gate_sha256,
            observation_path=arguments.observation,
            observation_sha256=arguments.observation_sha256,
            output_root=arguments.output_root,
        )
    except ProviderAcceptanceError as error:
        _parser().error(str(error))
    print(_canonical(summary).decode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        raise SystemExit(
            "Phase 6.6 provider evidence preparation stopped: an external operation failed closed"
        ) from None
