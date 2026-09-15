from hashlib import sha256
from urllib.parse import urlsplit

import pytest

from tools.prepare_judge_invitation import prepare_invitation, write_private


def test_invitation_is_fragment_only_and_record_contains_only_digest():
    link, row = prepare_invitation(
        campaign_id="hackathon-2026",
        owner_id="a" * 64,
        expires_at=1900,
        redemption_limit=50,
        now=1000,
    )
    url = urlsplit(link)
    assert url.scheme == "https" and url.netloc == "massskutiny.com"
    assert url.path == "/judge/" and not url.query
    token = url.fragment.removeprefix("access=")
    assert len(token) == 43
    assert row["PK"] == "INVITE#" + sha256(token.encode()).hexdigest()
    assert token not in repr(row)
    assert row["redemptions"] == 0 and row["redemption_limit"] == 50


@pytest.mark.parametrize(
    "patch",
    [
        {"campaign_id": "../oops"},
        {"owner_id": "owner"},
        {"expires_at": 1000},
        {"expires_at": 1000 + 32 * 86400},
        {"redemption_limit": 0},
        {"redemption_limit": 101},
        {"redemption_limit": True},
        {"expires_at": True},
    ],
)
def test_unbounded_or_malformed_invitation_is_rejected(patch):
    args = dict(
        campaign_id="demo", owner_id="a" * 64, expires_at=2000, redemption_limit=50, now=1000
    )
    with pytest.raises(ValueError):
        prepare_invitation(**{**args, **patch})


def test_private_output_is_exclusive_and_owner_only(tmp_path):
    path = tmp_path / "access.txt"
    write_private(path, "private")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private(path, "replacement")
    other = tmp_path / "symlink"
    other.symlink_to(path)
    with pytest.raises(FileExistsError):
        write_private(other, "replacement")
    assert path.read_text() == "private"
