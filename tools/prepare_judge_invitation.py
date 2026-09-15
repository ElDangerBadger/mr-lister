"""Prepare a bounded judge access link and DynamoDB record in private local files.

This performs no network call or deployment. Only the SHA-256 digest is uploaded to
the invitation table. The URL itself is an access credential and must not be committed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path


def prepare_invitation(
    *,
    campaign_id: str,
    owner_id: str,
    expires_at: int,
    redemption_limit: int,
    now: int | None = None,
) -> tuple[str, dict]:
    now = int(datetime.now(UTC).timestamp()) if now is None else now
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", campaign_id)
        or not re.fullmatch(r"[a-f0-9]{64}", owner_id)
        or type(expires_at) is not int
        or not now < expires_at <= now + 31 * 86400
        or type(redemption_limit) is not int
        or not 1 <= redemption_limit <= 100
    ):
        raise ValueError("Invitation requires exact campaign ownership and bounded future access")
    invitation = secrets.token_urlsafe(32)
    record = {
        "PK": "INVITE#" + sha256(invitation.encode("ascii")).hexdigest(),
        "contract_version": "judge-session-invitation-v1",
        "campaign_id": campaign_id,
        "owner_id": owner_id,
        "expires_at": expires_at,
        "redemption_limit": redemption_limit,
        "redemptions": 0,
        "enabled": True,
    }
    return f"https://massskutiny.com/judge/#access={invitation}", record


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--expires-at", type=int, required=True, help="UTC epoch seconds")
    parser.add_argument("--redemption-limit", type=int, default=50)
    parser.add_argument("--private-output-directory", type=Path, required=True)
    args = parser.parse_args()
    folder = args.private_output_directory.resolve()
    private = Path(".mr_lister_private").resolve()
    if not folder.is_relative_to(private):
        parser.error("Use a new directory under .mr_lister_private")
    url, record = prepare_invitation(
        campaign_id=args.campaign_id,
        owner_id=args.owner_id,
        expires_at=args.expires_at,
        redemption_limit=args.redemption_limit,
    )
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_private(folder / "invitation-record.json", json.dumps(record, indent=2) + "\n")
    write_private(folder / "project-url.txt", url + "\n")
    # Never echo the link or token in a terminal transcript.
    print("Private link and invitation record prepared; neither is deployed.")


if __name__ == "__main__":
    main()
