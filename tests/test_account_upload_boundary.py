from __future__ import annotations

import json

import pytest
from test_phase6_cloud_api import (
    OTHER_OWNER,
    OWNER,
    POLICY,
    UploadSpy,
    api_event,
    create_upload_body,
)

from mr_lister.cloud.api import UploadApiAdapter
from mr_lister.cloud.browser_contracts import CreateUploadRequest
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.connections.models import ConnectionConflict


def bound_store(owner: str = OWNER) -> StoreBindingAuthority:
    return StoreBindingAuthority.create(
        owner_id=owner,
        connection_id="conn_" + "a" * 32,
        shop_binding_id="binding_" + "b" * 32,
        shop_id=7,
        authorization_epoch=1,
    )


class BindingLookup:
    def __init__(self, value: object | None = None, error: Exception | None = None) -> None:
        self.value = value if value is not None else bound_store()
        self.error = error
        self.calls = []

    def get_active_binding(self, **values: object) -> StoreBindingAuthority:
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        return self.value


def request(**changes: object) -> dict:
    body = {
        **create_upload_body(),
        "shop_binding_id": "binding_" + "b" * 32,
        "expected_setup_version": 3,
    }
    body.update(changes)
    return api_event(
        "POST /v1/uploads",
        body=body,
        headers={"Content-Type": "application/json", "Idempotency-Key": "bound-intake-1"},
    )


def adapter(spy: UploadSpy, lookup: BindingLookup) -> UploadApiAdapter:
    return UploadApiAdapter(
        claims_policy=POLICY,
        uploads=spy,
        binding_authority=lookup,
        legacy_owner_ids=frozenset({OTHER_OWNER}),
    )


def test_server_resolves_only_owned_reference_and_passes_immutable_authority() -> None:
    spy, lookup = UploadSpy(), BindingLookup()
    response = adapter(spy, lookup)(request())
    assert response["statusCode"] == 201
    assert lookup.calls == [
        {
            "owner_id": OWNER,
            "shop_binding_id": "binding_" + "b" * 32,
            "expected_setup_version": 3,
        }
    ]
    values = spy.calls[0][1]
    assert values["store_binding"] == bound_store()
    assert "shop_binding_id" not in values and "expected_setup_version" not in values
    assert "store_binding" not in response["body"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", OTHER_OWNER),
        ("connection_id", "conn_" + "f" * 32),
        ("shop_id", 99),
        ("secret_arn", "private-secret-sentinel"),
        ("store_binding", bound_store(OTHER_OWNER).model_dump()),
    ],
)
def test_browser_cannot_supply_destination_authority(field: str, value: object) -> None:
    spy, lookup = UploadSpy(), BindingLookup()
    response = adapter(spy, lookup)(request(**{field: value}))
    assert response["statusCode"] == 422
    assert not spy.calls and not lookup.calls
    assert "private-secret-sentinel" not in response["body"]


def test_modern_owner_cannot_omit_store_reference_while_legacy_owner_can() -> None:
    spy, lookup = UploadSpy(), BindingLookup()
    event = request()
    event["body"] = json.dumps(create_upload_body())
    response = adapter(spy, lookup)(event)
    assert response["statusCode"] == 422 and not spy.calls and not lookup.calls
    legacy = UploadApiAdapter(
        claims_policy=POLICY,
        uploads=spy,
        binding_authority=lookup,
        legacy_owner_ids=frozenset({OWNER}),
    )
    assert legacy(event)["statusCode"] == 201
    assert "store_binding" not in spy.calls[0][1]
    assert not lookup.calls


@pytest.mark.parametrize("legacy_ids", [None, frozenset({OWNER}), frozenset({OTHER_OWNER})])
def test_native_account_group_never_uses_unbound_legacy_intake_after_rollback(legacy_ids) -> None:
    spy = UploadSpy()
    event = request()
    event["body"] = json.dumps(create_upload_body())
    event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = [
        "seller",
        "account",
    ]
    handler = UploadApiAdapter(
        claims_policy=POLICY,
        uploads=spy,
        legacy_owner_ids=legacy_ids,
    )
    assert handler(event)["statusCode"] == 422
    assert not spy.calls


def test_native_bound_reference_also_denies_when_connection_capability_is_disabled() -> None:
    spy = UploadSpy()
    event = request()
    event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = [
        "seller",
        "account",
    ]
    handler = UploadApiAdapter(
        claims_policy=POLICY,
        uploads=spy,
        legacy_owner_ids=frozenset({OTHER_OWNER}),
    )
    assert handler(event)["statusCode"] == 403
    assert not spy.calls


def test_native_group_comes_only_from_verified_authorizer_not_client_header() -> None:
    spy = UploadSpy()
    event = request()
    event["body"] = json.dumps(create_upload_body())
    event["headers"]["cognito:groups"] = "account"
    handler = UploadApiAdapter(claims_policy=POLICY, uploads=spy)
    assert handler(event)["statusCode"] == 201
    assert len(spy.calls) == 1


def test_authentication_precedes_binding_lookup() -> None:
    spy, lookup = UploadSpy(), BindingLookup()
    event = request()
    event["requestContext"].pop("authorizer")
    assert adapter(spy, lookup)(event)["statusCode"] == 401
    assert not spy.calls and not lookup.calls


@pytest.mark.parametrize("value", [bound_store(OTHER_OWNER), {"token": "private-token-sentinel"}])
def test_bad_dependency_authority_cannot_authorize_upload(value: object) -> None:
    spy, lookup = UploadSpy(), BindingLookup(value)
    response = adapter(spy, lookup)(request())
    assert response["statusCode"] == 503 and not spy.calls
    assert "private-token-sentinel" not in response["body"]


def test_changed_setup_is_a_value_free_actionable_validation_failure() -> None:
    spy, lookup = UploadSpy(), BindingLookup(error=ConnectionConflict())
    response = adapter(spy, lookup)(request())
    assert response["statusCode"] == 422 and not spy.calls
    assert json.loads(response["body"])["error"]["fields"][0]["path"] == "$.shop_binding_id"


@pytest.mark.parametrize(
    "changes",
    [
        {"shop_binding_id": None},
        {"expected_setup_version": None},
        {"expected_setup_version": True},
        {"expected_setup_version": 0},
        {"expected_setup_version": "3"},
        {"shop_binding_id": "7"},
    ],
)
def test_reference_pair_cannot_be_partial_or_coerced(changes: dict) -> None:
    values = json.loads(request(**changes)["body"])
    with pytest.raises(ValueError):
        CreateUploadRequest.model_validate(values)
