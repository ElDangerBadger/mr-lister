from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mr_lister.production.printify import PrintifyAuthenticationError
from mr_lister.production.provider_secrets import (
    MAX_PRINTIFY_API_TOKEN_CHARS,
    MAX_PRINTIFY_DELEGATED_OWNER_GRANTS,
    PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION,
    PRINTIFY_OWNER_SECRET_SCHEMA_VERSION,
    SecretsManagerOwnerPrintifyConnectionResolver,
)

SECRET_ARN = (
    "arn:aws:secretsmanager:us-west-2:123456789012:secret:mr-lister/dev/printify-owner-Ab12Cd"
)
OWNER = "a" * 64
OTHER_OWNER = "b" * 64
GENERIC_ERROR = "Owner-bound Printify credential is unavailable"
GRANT_EXPIRY = "2026-10-09T00:00:00Z"
BEFORE_EXPIRY = datetime(2026, 10, 8, tzinfo=UTC)


class RecordingSecretsManager:
    def __init__(
        self,
        responses: list[Mapping[str, Any]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def get_secret_value(self, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("Unexpected Secrets Manager call")
        return self.responses.pop(0)


def _secret_string(
    *,
    owner_id: str = OWNER,
    token: str = "printify-token-private",
    shop_id: object = 42,
    schema_version: str = PRINTIFY_OWNER_SECRET_SCHEMA_VERSION,
    extra: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "owner_id": owner_id,
        "shop_id": shop_id,
        "api_token": token,
    }
    payload.update(extra or {})
    return json.dumps(payload, separators=(",", ":"))


def _response(secret_string: str, **updates: object) -> dict[str, object]:
    response: dict[str, object] = {
        "ARN": SECRET_ARN,
        "VersionId": "version-one",
        "VersionStages": ["AWSCURRENT"],
        "SecretString": secret_string,
    }
    response.update(updates)
    return response


def _delegated_secret_string(
    *,
    grants: object,
    owner_id: str = OWNER,
    token: str = "printify-token-private",
    shop_id: object = 42,
) -> str:
    return _secret_string(
        owner_id=owner_id,
        token=token,
        shop_id=shop_id,
        schema_version=PRINTIFY_DELEGATED_OWNER_SECRET_SCHEMA_VERSION,
        extra={"delegated_owner_grants": grants},
    )


def _resolver(client: RecordingSecretsManager) -> SecretsManagerOwnerPrintifyConnectionResolver:
    return SecretsManagerOwnerPrintifyConnectionResolver(client=client, secret_arn=SECRET_ARN)


def _assert_generic_failure(
    resolver: SecretsManagerOwnerPrintifyConnectionResolver,
    *,
    owner_id: str = OWNER,
) -> PrintifyAuthenticationError:
    with pytest.raises(PrintifyAuthenticationError) as captured:
        resolver.resolve(owner_id=owner_id)
    assert str(captured.value) == GENERIC_ERROR
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    return captured.value


def test_exact_secret_request_returns_owner_bound_masked_connection() -> None:
    token = "printify-token-private"
    client = RecordingSecretsManager([_response(_secret_string(token=token))])

    connection = _resolver(client).resolve(owner_id=OWNER)

    assert client.requests == [{"SecretId": SECRET_ARN}]
    assert connection.owner_id == OWNER
    assert connection.shop_id == 42
    assert connection.api_token.get_secret_value() == token
    assert token not in repr(connection)
    assert token not in str(connection.model_dump())
    assert token not in connection.model_dump_json()


def test_every_resolve_reads_fresh_and_observes_secret_rotation() -> None:
    client = RecordingSecretsManager(
        [
            _response(_secret_string(token="token-before-rotation")),
            _response(
                _secret_string(token="token-after-rotation"),
                VersionId="version-two",
            ),
        ]
    )
    resolver = _resolver(client)

    first = resolver.resolve(owner_id=OWNER)
    second = resolver.resolve(owner_id=OWNER)

    assert first.api_token.get_secret_value() == "token-before-rotation"
    assert second.api_token.get_secret_value() == "token-after-rotation"
    assert client.requests == [{"SecretId": SECRET_ARN}, {"SecretId": SECRET_ARN}]


def test_wrong_owner_is_indistinguishable_from_malformed_secret() -> None:
    wrong_owner = _resolver(
        RecordingSecretsManager([_response(_secret_string(owner_id=OTHER_OWNER))])
    )
    malformed = _resolver(RecordingSecretsManager([_response("not-json")]))

    wrong_error = _assert_generic_failure(wrong_owner)
    malformed_error = _assert_generic_failure(malformed)

    assert type(wrong_error) is type(malformed_error)
    assert str(wrong_error) == str(malformed_error)
    assert OTHER_OWNER not in str(wrong_error)


@pytest.mark.parametrize(
    "secret_string",
    [
        "null",
        "[]",
        "not-json",
        '{"schema_version":"phase6-printify-owner-v1"}',
        _secret_string(extra={"unexpected": True}),
        _secret_string(schema_version="phase6-printify-owner-v2"),
        _secret_string(owner_id="root"),
        _secret_string(owner_id="shared"),
        _secret_string(owner_id="*"),
        _secret_string(owner_id="0" * 64),
        _secret_string(owner_id=OWNER.upper()),
        _secret_string(shop_id=0),
        _secret_string(shop_id=-1),
        _secret_string(shop_id=True),
        _secret_string(shop_id="42"),
        _secret_string(token=""),
        _secret_string(token=" token"),
        _secret_string(token="token "),
        _secret_string(token="token\nvalue"),
        _secret_string(token="töken"),
        _secret_string(token="x" * (MAX_PRINTIFY_API_TOKEN_CHARS + 1)),
        (
            '{"schema_version":"phase6-printify-owner-v1",'
            f'"owner_id":"{OWNER}","shop_id":42,'
            '"api_token":"token-one","api_token":"token-two"}'
        ),
        (
            '{"schema_version":"phase6-printify-owner-v1",'
            f'"owner_id":"{OWNER}","shop_id":NaN,"api_token":"token"}}'
        ),
    ],
)
def test_malformed_or_ambiguous_secret_json_fails_with_one_safe_error(
    secret_string: str,
) -> None:
    resolver = _resolver(RecordingSecretsManager([_response(secret_string)]))

    error = _assert_generic_failure(resolver)

    assert secret_string not in str(error)


@pytest.mark.parametrize(
    "response",
    [
        {"SecretBinary": b"binary-token"},
        {
            "SecretString": _secret_string(),
            "SecretBinary": b"binary-token",
        },
        {"SecretString": ""},
        {"SecretString": b"bytes-are-not-accepted"},
        {},
        [],
    ],
)
def test_binary_missing_or_nonstring_envelopes_are_rejected(response: object) -> None:
    client = RecordingSecretsManager([response])  # type: ignore[list-item]

    _assert_generic_failure(_resolver(client))


@pytest.mark.parametrize(
    "updates",
    [
        {"ARN": "arn:aws:secretsmanager:us-west-2:123456789012:secret:other-Ab12Cd"},
        {"ARN": ""},
        {"ARN": None},
        {"VersionStages": []},
        {"VersionStages": None},
        {"VersionStages": ["AWSPREVIOUS"]},
        {"VersionStages": ["AWSCURRENT", "AWSPENDING"]},
        {"VersionStages": "AWSCURRENT"},
    ],
)
def test_response_arn_or_version_stage_drift_is_rejected(updates: dict[str, object]) -> None:
    resolver = _resolver(RecordingSecretsManager([_response(_secret_string(), **updates)]))

    _assert_generic_failure(resolver)


def test_optional_aws_envelope_metadata_may_be_absent() -> None:
    client = RecordingSecretsManager([{"SecretString": _secret_string()}])

    connection = _resolver(client).resolve(owner_id=OWNER)

    assert connection.owner_id == OWNER
    assert connection.shop_id == 42


def test_dependency_error_text_and_chain_are_suppressed() -> None:
    sensitive_error = RuntimeError("provider failed with printify-token-private")
    resolver = _resolver(RecordingSecretsManager(error=sensitive_error))

    error = _assert_generic_failure(resolver)

    assert "provider failed" not in str(error)
    assert "printify-token-private" not in str(error)


def test_invalid_requested_owner_fails_without_reading_the_secret() -> None:
    client = RecordingSecretsManager([_response(_secret_string())])

    _assert_generic_failure(_resolver(client), owner_id="shared")

    assert client.requests == []


@pytest.mark.parametrize(
    "secret_arn",
    [
        "",
        " ",
        "mr-lister/dev/printify-owner",
        "arn:aws:ssm:us-west-2:123456789012:parameter/mr-lister/printify",
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:*",
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:mr-lister/dev/owner",
        (
            "arn:aws:secretsmanager:us-west-2:123456789012:"
            "secret:mr-lister/dev/owner-Ab12Cd:AWSCURRENT"
        ),
        None,
    ],
)
def test_configured_secret_arn_must_be_one_exact_nonblank_arn(secret_arn: object) -> None:
    with pytest.raises(ValueError, match="exact"):
        SecretsManagerOwnerPrintifyConnectionResolver(
            client=RecordingSecretsManager([]),
            secret_arn=secret_arn,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "secret_arn",
    [
        SECRET_ARN,
        (
            "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:"
            "secret:mr-lister/prod/owner-Zy98Xw"
        ),
        ("arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:mr-lister/prod/owner-Zy98Xw"),
    ],
)
def test_exact_supported_partition_secret_arns_are_accepted(secret_arn: str) -> None:
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=RecordingSecretsManager([]),
        secret_arn=secret_arn,
    )

    assert repr(resolver).startswith("<mr_lister.production.provider_secrets.")


def test_delegation_shares_connection_but_preserves_each_requested_owner_identity() -> None:
    secret = _delegated_secret_string(
        grants=[{"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY}]
    )
    client = RecordingSecretsManager([_response(secret), _response(secret)])
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=client, secret_arn=SECRET_ARN, clock=lambda: BEFORE_EXPIRY
    )

    primary = resolver.resolve(owner_id=OWNER)
    delegated = resolver.resolve(owner_id=OTHER_OWNER)

    assert primary.owner_id == OWNER
    assert delegated.owner_id == OTHER_OWNER
    assert primary.shop_id == delegated.shop_id == 42
    assert primary.api_token == delegated.api_token
    assert "printify-token-private" not in delegated.model_dump_json()
    assert client.requests == [{"SecretId": SECRET_ARN}] * 2


@pytest.mark.parametrize("offset_seconds", [-1, 0, 1])
def test_grant_expires_at_exact_utc_boundary_without_expiring_primary_owner(
    offset_seconds: int,
) -> None:
    secret = _delegated_secret_string(
        grants=[{"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY}]
    )
    now = datetime(2026, 10, 9, tzinfo=UTC) + timedelta(seconds=offset_seconds)
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=RecordingSecretsManager([_response(secret), _response(secret)]),
        secret_arn=SECRET_ARN,
        clock=lambda: now,
    )

    assert resolver.resolve(owner_id=OWNER).owner_id == OWNER
    if offset_seconds < 0:
        assert resolver.resolve(owner_id=OTHER_OWNER).owner_id == OTHER_OWNER
    else:
        _assert_generic_failure(resolver, owner_id=OTHER_OWNER)


def test_every_resolve_observes_delegation_removal_expiry_and_v1_rollback() -> None:
    client = RecordingSecretsManager(
        [
            _response(
                _delegated_secret_string(
                    grants=[{"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY}]
                )
            ),
            _response(_delegated_secret_string(grants=[])),
            _response(
                _delegated_secret_string(
                    grants=[{"owner_id": OTHER_OWNER, "expires_at": "2026-10-08T00:00:00Z"}]
                )
            ),
            _response(_secret_string()),
        ]
    )
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=client, secret_arn=SECRET_ARN, clock=lambda: BEFORE_EXPIRY
    )

    assert resolver.resolve(owner_id=OTHER_OWNER).owner_id == OTHER_OWNER
    for _ in range(3):
        _assert_generic_failure(resolver, owner_id=OTHER_OWNER)
    assert client.requests == [{"SecretId": SECRET_ARN}] * 4


def test_empty_grant_list_preserves_primary_and_rejects_other_owners() -> None:
    secret = _delegated_secret_string(grants=[])
    resolver = _resolver(RecordingSecretsManager([_response(secret), _response(secret)]))

    assert resolver.resolve(owner_id=OWNER).owner_id == OWNER
    _assert_generic_failure(resolver, owner_id=OTHER_OWNER)


@pytest.mark.parametrize(
    "grants",
    [
        None,
        {},
        "*",
        [None],
        [{"owner_id": OTHER_OWNER}],
        [{"expires_at": GRANT_EXPIRY}],
        [{"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY, "publish": True}],
        [
            {"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY},
            {"owner_id": OTHER_OWNER, "expires_at": "2026-10-10T00:00:00Z"},
        ],
        [
            {"owner_id": f"{index:064x}", "expires_at": GRANT_EXPIRY}
            for index in range(1, MAX_PRINTIFY_DELEGATED_OWNER_GRANTS + 2)
        ],
    ]
    + [
        [{"owner_id": owner, "expires_at": GRANT_EXPIRY}]
        for owner in [OWNER, "0" * 64, OTHER_OWNER.upper(), "*", "shared", "", None, True, 42]
    ]
    + [
        [{"owner_id": OTHER_OWNER, "expires_at": expiry}]
        for expiry in [
            None,
            True,
            1_791_504_000,
            "",
            "never",
            "2026-10-09",
            "2026-10-09T00:00:00",
            "2026-10-09T00:00:00+00:00",
            "2026-10-09T00:00:00.000Z",
            "2026-02-30T00:00:00Z",
            "2026-10-09T24:00:00Z",
            "2026-10-09T00:00:60Z",
            "2026-1-9T00:00:00Z",
            "2026-10-09T00:00:00Z\n",
        ]
    ],
)
def test_malformed_or_ambiguous_grants_fail_closed_even_for_primary(grants: object) -> None:
    secret = _delegated_secret_string(grants=grants)
    resolver = _resolver(RecordingSecretsManager([_response(secret), _response(secret)]))

    _assert_generic_failure(resolver, owner_id=OWNER)
    _assert_generic_failure(resolver, owner_id=OTHER_OWNER)


def test_maximum_grant_count_is_accepted_and_remains_explicit() -> None:
    grants = [
        {"owner_id": f"{index:064x}", "expires_at": GRANT_EXPIRY}
        for index in range(1, MAX_PRINTIFY_DELEGATED_OWNER_GRANTS + 1)
    ]
    secret = _delegated_secret_string(grants=grants)
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=RecordingSecretsManager([_response(secret), _response(secret)]),
        secret_arn=SECRET_ARN,
        clock=lambda: BEFORE_EXPIRY,
    )

    assert resolver.resolve(owner_id=grants[-1]["owner_id"]).owner_id == grants[-1]["owner_id"]
    _assert_generic_failure(resolver, owner_id=OTHER_OWNER)


def test_duplicate_nested_grant_json_key_is_rejected() -> None:
    secret = _delegated_secret_string(
        grants=[{"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY}]
    ).replace(
        f'"expires_at":"{GRANT_EXPIRY}"',
        f'"expires_at":"{GRANT_EXPIRY}","expires_at":"2099-01-01T00:00:00Z"',
    )

    _assert_generic_failure(_resolver(RecordingSecretsManager([_response(secret)])))


@pytest.mark.parametrize("now", [datetime(2026, 10, 8), None, "2026-10-08T00:00:00Z"])
def test_delegation_rejects_untrusted_clock_result(now: object) -> None:
    secret = _delegated_secret_string(
        grants=[{"owner_id": OTHER_OWNER, "expires_at": GRANT_EXPIRY}]
    )
    resolver = SecretsManagerOwnerPrintifyConnectionResolver(
        client=RecordingSecretsManager([_response(secret)]),
        secret_arn=SECRET_ARN,
        clock=lambda: now,  # type: ignore[arg-type,return-value]
    )

    _assert_generic_failure(resolver, owner_id=OTHER_OWNER)
