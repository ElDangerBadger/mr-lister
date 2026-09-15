"""Prepare the bounded judge login/SPA patch from a captured live Phase6 template.

This local tool does not deploy, create accounts, or read credentials. The dedicated
judge federation stack must exist before applying this application configuration.
"""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from tools.bind_phase6_runtime_config import APPLICATION_ORIGIN

_SPA_LINE = (
    "  var isSpaRoute = uri === '/' || uri === '/auth/callback' || uri === '/jobs' "
    "|| uri.indexOf('/jobs/') === 0 || uri.indexOf('/uploads/') === 0;"
)
_JUDGE_SPA_LINE = (
    "  var isJudgeRoute = uri === '/judge' || uri === '/judge/' "
    "|| uri === '/judge/auth/callback' || uri === '/judge/signout' "
    "|| uri === '/judge/jobs' || uri.indexOf('/judge/jobs/') === 0 "
    "|| uri.indexOf('/judge/uploads/') === 0;"
)


def prepare_application_template(baseline: dict, *, origin: str) -> dict:
    """Preserve API, owner MFA, IAM, and runtime while adding exact judge UI routes."""
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port
        or origin != f"https://{parsed.hostname}"
        or origin != APPLICATION_ORIGIN
    ):
        raise ValueError("The existing application's exact HTTPS origin is required")
    template = deepcopy(baseline)
    resources = template["Resources"]
    if resources["SellerUserPool"]["Properties"]["MfaConfiguration"] != "ON":
        raise ValueError("The existing seller pool must retain required MFA")
    client = resources["SellerUserPoolClient"]["Properties"]
    # Omitted permissions use Cognito's existing standard-attribute defaults.
    writable = client.get("WriteAttributes")
    if writable is not None and not {"email", "email_verified"}.issubset(writable):
        raise ValueError("The broker's mapped attributes must already be writable")
    providers = client.setdefault("SupportedIdentityProviders", ["COGNITO"])
    if "COGNITO" not in providers or "MrListerJudge" in providers:
        raise ValueError("Unexpected existing identity provider configuration")
    providers.append("MrListerJudge")
    additions = {
        "CallbackURLs": [f"{origin}/judge/auth/callback"],
        "LogoutURLs": [f"{origin}/judge/", f"{origin}/judge/signout"],
    }
    for field, values in additions.items():
        if not isinstance(client.get(field), list) or not client[field]:
            raise ValueError("Existing seller redirect configuration is missing")
        for value in values:
            if value in client[field]:
                raise ValueError("Judge redirect already present")
            client[field].append(value)

    function = resources["SellerSpaRouteFunction"]["Properties"]
    code = function["FunctionCode"]
    if code.count(_SPA_LINE) != 1 or "isJudgeRoute" in code:
        raise ValueError("SPA routing differs from the reviewed application")
    function["FunctionCode"] = code.replace(
        _SPA_LINE,
        _SPA_LINE + "\n" + _JUDGE_SPA_LINE + "\n  isSpaRoute = isSpaRoute || isJudgeRoute;",
    )

    # Use the existing no-store policy, keeping the runtime JSON out of SPA rewrites.
    behaviors = resources["SellerWebDistribution"]["Properties"]["DistributionConfig"][
        "CacheBehaviors"
    ]
    matches = [item for item in behaviors if item.get("PathPattern") == "/runtime-config.json"]
    if len(matches) != 1 or any(
        item.get("PathPattern") == "/judge/runtime-config.json" for item in behaviors
    ):
        raise ValueError("Unexpected runtime configuration cache behavior")
    judge_behavior = deepcopy(matches[0])
    judge_behavior["PathPattern"] = "/judge/runtime-config.json"
    behaviors.insert(behaviors.index(matches[0]) + 1, judge_behavior)
    return template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-template", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    template = prepare_application_template(
        json.loads(args.baseline_template.read_text()), origin=args.origin
    )
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(template, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
