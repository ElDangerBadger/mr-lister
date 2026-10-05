from __future__ import annotations

import pytest
from pydantic import ValidationError

from mr_lister.connections.binding import StoreBindingAuthority


def binding(**changes: object) -> StoreBindingAuthority:
    values = {
        "owner_id": "a" * 64, "connection_id": "conn_" + "b" * 32,
        "shop_binding_id": "binding_" + "c" * 32, "shop_id": 123,
        "authorization_epoch": 1,
    }
    values.update(changes)
    return StoreBindingAuthority.create(**values)


def test_destination_is_stable_across_authorization_epochs() -> None:
    first, renewed = binding(), binding(authorization_epoch=2)
    assert first.binding_fingerprint == renewed.binding_fingerprint
    assert first.authorization_epoch != renewed.authorization_epoch
    assert first.checked_for_owner("a" * 64) == first


@pytest.mark.parametrize("field,value", [
    ("owner_id", "d" * 64), ("connection_id", "conn_" + "d" * 32),
    ("shop_binding_id", "binding_" + "d" * 32), ("shop_id", 456),
])
def test_every_destination_identity_changes_the_fingerprint(field: str, value: object) -> None:
    first = binding()
    assert binding(**{field: value}).binding_fingerprint != first.binding_fingerprint
    changed = first.model_dump(mode="python")
    changed[field] = value
    with pytest.raises(ValidationError):
        StoreBindingAuthority.model_validate(changed)


@pytest.mark.parametrize("field,value", [
    ("owner_id", "user@example.com"), ("connection_id", "legacy-owner"),
    ("shop_binding_id", "123"), ("shop_id", True), ("shop_id", "123"),
    ("shop_id", 0), ("authorization_epoch", 0), ("authorization_epoch", True),
])
def test_binding_rejects_inferred_or_coerced_authority(field: str, value: object) -> None:
    with pytest.raises((ValidationError, TypeError)):
        binding(**{field: value})


def test_other_owner_and_unvalidated_model_copy_do_not_acquire_authority() -> None:
    first = binding()
    with pytest.raises(ValueError):
        first.checked_for_owner("d" * 64)
    modified = first.model_copy(update={"shop_id": 456})
    with pytest.raises(ValidationError):
        modified.checked_for_owner("a" * 64)


@pytest.mark.parametrize("field", ["api_token", "secret_arn", "refresh_token"])
def test_credential_material_has_no_place_in_a_binding(field: str) -> None:
    values = binding().model_dump(mode="python")
    values[field] = "private-token-sentinel"
    with pytest.raises(ValidationError) as caught:
        StoreBindingAuthority.model_validate(values)
    assert "private-token-sentinel" not in str(caught.value)
