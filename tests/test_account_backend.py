from __future__ import annotations

import copy
import json
import traceback
from hashlib import sha256
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from mr_lister.accounts import provision_entrypoint, query_entrypoint
from mr_lister.accounts.models import (
    ACCOUNT_ENTITY,
    ACCOUNT_SCOPE,
    PREFIX,
    AccountConfig,
    AccountIdentity,
    AccountUnavailable,
    MerchantAccount,
    owner_id_for,
)
from mr_lister.accounts.provision import (
    PRE_AUTHENTICATION,
    AccountProvisioner,
    CognitoAccountGroupWriter,
    native_signup,
)
from mr_lister.accounts.query import AccountQueryHandler
from mr_lister.accounts.store import DynamoAccountReader, DynamoAccountWriter, account_key
from mr_lister.cloud.auth import AccessDeniedError, SellerClaimsPolicy, authenticate_seller

SUBJECT = "11111111-2222-4333-8444-555555555555"
OTHER_SUBJECT = "22222222-3333-4444-8555-666666666666"
SENTINEL = "private-password-token-and-email@example.invalid"
NOW = 1_800_000_000


@pytest.fixture
def config() -> AccountConfig:
    return AccountConfig(
        user_pool_id="us-west-2_ExamplePool",
        client_id="accountclient123",
        table_name="account-test",
        reserved_owner_ids=frozenset({"f" * 64}),
    )


def confirmation(config: AccountConfig, subject: str = SUBJECT) -> dict:
    return {
        "version": "1",
        "triggerSource": "PostConfirmation_ConfirmSignUp",
        "region": config.region,
        "userPoolId": config.user_pool_id,
        "userName": subject,
        "callerContext": {"awsSdkVersion": "aws-sdk-test", "clientId": config.client_id},
        "request": {
            "userAttributes": {
                "sub": subject,
                "email": "new-merchant@example.invalid",
                "email_verified": "true",
                "cognito:user_status": "CONFIRMED",
            },
            "clientMetadata": {},
        },
        "response": {},
    }


def pre_authentication(config: AccountConfig, subject: str = SUBJECT) -> dict:
    event = confirmation(config, subject)
    event["triggerSource"] = PRE_AUTHENTICATION
    event["request"].pop("clientMetadata")
    event["request"].update(userNotFound=False, validationData={})
    return event


def account(
    config: AccountConfig, subject: str = SUBJECT, created_at: int = NOW
) -> MerchantAccount:
    signup = native_signup(confirmation(config, subject), config)
    assert signup is not None
    return MerchantAccount(identity=signup.identity, created_at=created_at)


def query_event(config: AccountConfig, subject: str = SUBJECT, groups: object = None) -> dict:
    return {
        "version": "2.0",
        "routeKey": "GET /v1/account",
        "rawPath": "/v1/account",
        "rawQueryString": "",
        "isBase64Encoded": False,
        "headers": {"authorization": f"Bearer {SENTINEL}"},
        "requestContext": {
            "requestId": "safe-request",
            "http": {"method": "GET", "path": "/v1/account"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "iss": config.issuer,
                        "client_id": config.client_id,
                        "sub": subject,
                        "token_use": "access",
                        "scope": f"openid {ACCOUNT_SCOPE}",
                        "cognito:groups": ["account"] if groups is None else groups,
                    }
                }
            },
        },
    }


def environment(config: AccountConfig) -> dict:
    return {
        PREFIX + "USER_POOL_ID": config.user_pool_id,
        PREFIX + "CLIENT_ID": config.client_id,
        PREFIX + "TABLE_NAME": config.table_name,
        PREFIX + "RESERVED_OWNER_IDS": json.dumps(sorted(config.reserved_owner_ids)),
    }


def envelope(value: MerchantAccount) -> dict:
    return {
        "PK": {"S": account_key(value.owner_id)},
        "entity_type": {"S": ACCOUNT_ENTITY},
        "payload": {"S": json.dumps(value.payload(), sort_keys=True, separators=(",", ":"))},
    }


class DynamoFake:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.puts: list[dict] = []
        self.gets: list[dict] = []
        self.lose_next_put_response = False

    def get_item(self, **kwargs: object) -> dict:
        self.gets.append(copy.deepcopy(kwargs))
        assert kwargs["ConsistentRead"] is True
        key = kwargs["Key"]["PK"]["S"]
        return {"Item": copy.deepcopy(self.items[key])} if key in self.items else {}

    def put_item(self, **kwargs: object) -> dict:
        self.puts.append(copy.deepcopy(kwargs))
        assert kwargs["ConditionExpression"] == "attribute_not_exists(PK)"
        key = kwargs["Item"]["PK"]["S"]
        if key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": SENTINEL}},
                "PutItem",
            )
        self.items[key] = copy.deepcopy(kwargs["Item"])
        if self.lose_next_put_response:
            self.lose_next_put_response = False
            raise RuntimeError(SENTINEL)
        return {}


class CognitoFake:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.memberships: set[tuple[str, str, str]] = set()
        self.lose_next_response = False
        self.reads: list[dict] = []

    def admin_get_user(self, **kwargs: str) -> dict:
        self.reads.append(kwargs)
        return {
            "Enabled": True,
            "UserStatus": "CONFIRMED",
            "Username": kwargs["Username"],
            "UserAttributes": [
                {"Name": "sub", "Value": kwargs["Username"]},
                {"Name": "email", "Value": "new-merchant@example.invalid"},
                {"Name": "email_verified", "Value": "true"},
            ],
        }

    def admin_add_user_to_group(self, **kwargs: str) -> dict:
        self.calls.append(kwargs)
        assert kwargs["GroupName"] == "account"
        self.memberships.add((kwargs["UserPoolId"], kwargs["Username"], kwargs["GroupName"]))
        if self.lose_next_response:
            self.lose_next_response = False
            raise RuntimeError(SENTINEL)
        return {}


def provisioner(config: AccountConfig, dynamo: DynamoFake, cognito: CognitoFake, now: int = NOW):
    return AccountProvisioner(
        config,
        store=DynamoAccountWriter(dynamo, config),
        groups=CognitoAccountGroupWriter(cognito, config),
        clock=lambda: now,
    )


def test_verified_native_confirmation_creates_immutable_account_and_only_account_membership(config):
    dynamo, cognito = DynamoFake(), CognitoFake()
    signup = native_signup(confirmation(config), config)
    result = provisioner(config, dynamo, cognito).provision(signup)
    expected_owner = sha256(config.issuer.encode() + b"\0" + SUBJECT.encode()).hexdigest()
    assert result.owner_id == expected_owner
    assert result.payload()["entitlements"] == ["manage_connections"]
    assert cognito.calls == [
        {"UserPoolId": config.user_pool_id, "Username": SUBJECT, "GroupName": "account"}
    ]
    assert dynamo.puts[0]["Item"] == envelope(result)
    assert "email" not in json.dumps(result.payload())
    assert "new-merchant" not in json.dumps(result.payload())
    assert result.public_projection() == {
        "contract_version": "account-setup-v1",
        "record_version": 1,
        "state": "connection_unavailable",
        "connection_method": "unavailable",
        "store": None,
    }
    with pytest.raises(AccessDeniedError):
        authenticate_seller(
            query_event(config),
            policy=SellerClaimsPolicy(
                issuer=config.issuer,
                client_id=config.client_id,
                required_scope=ACCOUNT_SCOPE,
            ),
        )


@pytest.mark.parametrize("verified", ["true", True])
def test_native_email_username_and_both_documented_true_representations(config, verified):
    event = confirmation(config)
    event["userName"] = event["request"]["userAttributes"]["email"]
    event["request"]["userAttributes"]["email_verified"] = verified
    signup = native_signup(event, config)
    assert signup is not None
    assert signup.identity.owner_id == owner_id_for(config.issuer, SUBJECT)
    assert signup.identity.username_digest == sha256(event["userName"].encode()).hexdigest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("email_verified", "false"),
        ("email_verified", False),
        ("email_verified", 1),
        ("email_verified", "TRUE"),
        ("email_verified", None),
        ("email", SENTINEL + "\n"),
        ("email", "missing-domain"),
        ("sub", "MrListerJudge_external"),
        ("sub", None),
        ("cognito:user_status", "UNCONFIRMED"),
    ],
)
def test_invalid_native_attributes_fail_before_provisioning(config, field, value):
    event = confirmation(config)
    event["request"]["userAttributes"][field] = value
    with pytest.raises(ValueError, match="Invalid account"):
        native_signup(event, config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("userPoolId", "us-west-2_OtherPool"),
        ("region", "us-east-1"),
        ("version", "2"),
        ("triggerSource", "PreSignUp_SignUp"),
        ("userName", "MrListerJudge_" + SUBJECT),
        ("userName", "other@example.invalid"),
        ("response", {"group": "seller"}),
    ],
)
def test_mismatched_trigger_authority_is_rejected(config, field, value):
    event = confirmation(config)
    event[field] = value
    with pytest.raises(ValueError, match="Invalid account"):
        native_signup(event, config)


def test_wrong_client_is_rejected_and_client_metadata_cannot_choose_authority(config):
    event = confirmation(config)
    event["callerContext"]["clientId"] = "wrongclient"
    with pytest.raises(ValueError):
        native_signup(event, config)
    event = confirmation(config)
    event["owner_id"] = "a" * 64
    event["group"] = "seller"
    event["request"]["clientMetadata"] = {
        "owner_id": "a" * 64,
        "group": "seller",
        "entitlements": "seller",
        "table_name": "victim",
        "api_token": SENTINEL,
    }
    dynamo, cognito = DynamoFake(), CognitoFake()
    stored = provisioner(config, dynamo, cognito).provision(native_signup(event, config))
    assert stored.owner_id == owner_id_for(config.issuer, SUBJECT)
    assert cognito.calls[0]["GroupName"] == "account"
    assert SENTINEL not in json.dumps(dynamo.items)
    assert SENTINEL not in repr(cognito.calls)


@pytest.mark.parametrize(
    "identities", ["[]", '[{"providerName":"MrListerJudge"}]', "invalid", None]
)
def test_linked_or_federated_identity_metadata_is_always_skipped(config, identities):
    event = confirmation(config)
    event["request"]["userAttributes"]["identities"] = identities
    assert native_signup(event, config) is None


def test_forgot_password_and_external_provider_confirmation_do_not_provision(config):
    event = confirmation(config)
    event["triggerSource"] = "PostConfirmation_ConfirmForgotPassword"
    assert native_signup(event, config) is None
    event = confirmation(config)
    event["request"]["userAttributes"]["cognito:user_status"] = "EXTERNAL_PROVIDER"
    assert native_signup(event, config) is None


def test_reserved_legacy_owner_cannot_be_enrolled_even_with_native_confirmation(config):
    protected = AccountConfig(
        config.user_pool_id,
        config.client_id,
        config.table_name,
        frozenset({owner_id_for(config.issuer, SUBJECT)}),
    )
    with pytest.raises(ValueError, match="Invalid account identity"):
        native_signup(confirmation(protected), protected)


@pytest.mark.parametrize("lost_step", ["none", "record", "group"])
def test_replay_recovers_partial_and_unknown_results_without_overwrite(config, lost_step):
    dynamo, cognito = DynamoFake(), CognitoFake()
    signup = native_signup(confirmation(config), config)
    dynamo.lose_next_put_response = lost_step == "record"
    cognito.lose_next_response = lost_step == "group"
    first = provisioner(config, dynamo, cognito)
    if lost_step == "none":
        first.provision(signup)
    else:
        with pytest.raises(AccountUnavailable) as error:
            first.provision(signup)
        assert SENTINEL not in "".join(traceback.format_exception(error.value))
        assert error.value.__context__ is None
    original_row = copy.deepcopy(dynamo.items)
    retried = provisioner(config, dynamo, cognito, now=NOW + 123).provision(signup)
    assert retried.created_at == NOW
    assert dynamo.items == original_row
    assert len(dynamo.items) == 1
    assert len(cognito.memberships) == 1
    assert all(call["GroupName"] == "account" for call in cognito.calls)


@pytest.mark.parametrize("collision", ["other_username", "legacy", "malformed"])
@pytest.mark.parametrize("event_factory", [confirmation, pre_authentication])
def test_existing_conflicting_or_legacy_account_is_never_overwritten_or_granted(
    config, collision, event_factory
):
    dynamo, cognito = DynamoFake(), CognitoFake()
    original = account(config)
    row = envelope(original)
    if collision == "other_username":
        data = original.payload()
        data["username_digest"] = "e" * 64
        row["payload"] = {"S": json.dumps(data)}
    elif collision == "legacy":
        row["entity_type"] = {"S": "LEGACY_OWNER"}
    else:
        row["payload"] = {"S": SENTINEL}
    dynamo.items[account_key(original.owner_id)] = row
    before = copy.deepcopy(dynamo.items)
    with pytest.raises(AccountUnavailable):
        provisioner(config, dynamo, cognito).provision(native_signup(event_factory(config), config))
    assert dynamo.items == before
    assert cognito.calls == []


def test_concurrent_creators_converge_on_one_original_record(config):
    dynamo = DynamoFake()
    earlier = account(config, created_at=NOW - 1)
    writer = DynamoAccountWriter(dynamo, config)
    # The competing write wins after this caller prepared its later candidate.
    later = account(config)
    writer.create_if_absent(earlier)
    winner = writer.create_if_absent(later)
    assert winner == earlier
    assert len(dynamo.items) == 1


def test_different_native_owners_get_isolated_keys_and_same_safe_projection(config):
    dynamo, cognito = DynamoFake(), CognitoFake()
    service = provisioner(config, dynamo, cognito)
    first = service.provision(native_signup(confirmation(config), config))
    second = service.provision(native_signup(confirmation(config, OTHER_SUBJECT), config))
    assert first.owner_id != second.owner_id
    reader = DynamoAccountReader(dynamo, config)
    handler = AccountQueryHandler(config, reader_factory=lambda: reader)
    for subject, expected in [(SUBJECT, first), (OTHER_SUBJECT, second)]:
        result = handler(query_event(config, subject))
        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == expected.public_projection()
        assert dynamo.gets[-1]["Key"] == {"PK": {"S": account_key(expected.owner_id)}}
        for private in [subject, expected.owner_id, config.client_id, SENTINEL]:
            assert private not in json.dumps(result)
        assert "no-store" in result["headers"]["Cache-Control"]


@pytest.mark.parametrize(
    "groups", [["seller"], ["seller", "us-west-2_ExamplePool_MrListerJudge"], [], "bad group"]
)
def test_seller_judge_and_missing_account_group_are_denied_before_reader(config, groups):
    factory = Mock(side_effect=AssertionError("reader must not be built"))
    result = AccountQueryHandler(config, reader_factory=factory)(query_event(config, groups=groups))
    assert result["statusCode"] == 403
    factory.assert_not_called()


def test_reserved_owner_is_denied_even_if_account_group_was_accidentally_assigned(config):
    protected = AccountConfig(
        config.user_pool_id,
        config.client_id,
        config.table_name,
        frozenset({owner_id_for(config.issuer, SUBJECT)}),
    )
    factory = Mock(side_effect=AssertionError("reader must not be built"))
    assert (
        AccountQueryHandler(protected, reader_factory=factory)(query_event(protected))["statusCode"]
        == 403
    )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_OtherPool"),
        ("client_id", "wrongclient"),
        ("token_use", "id"),
        ("scope", "openid"),
        ("sub", "invalid/subject"),
    ],
)
def test_query_rejects_mismatched_verified_claims_before_dependency(config, field, value):
    event = query_event(config)
    event["requestContext"]["authorizer"]["jwt"]["claims"][field] = value
    factory = Mock()
    result = AccountQueryHandler(config, reader_factory=factory)(event)
    assert result["statusCode"] == 403
    factory.assert_not_called()


def test_authorization_header_alone_cannot_authenticate_query(config):
    event = query_event(config)
    del event["requestContext"]["authorizer"]
    factory = Mock()
    result = AccountQueryHandler(config, reader_factory=factory)(event)
    assert result["statusCode"] == 401
    assert SENTINEL not in json.dumps(result)
    factory.assert_not_called()


@pytest.mark.parametrize(
    "change,status",
    [
        ({"body": json.dumps({"owner_id": "a" * 64, "token": SENTINEL})}, 400),
        ({"rawQueryString": "owner_id=" + "a" * 64}, 400),
        ({"queryStringParameters": {"owner_id": "a" * 64}}, 400),
        ({"pathParameters": {"owner_id": "a" * 64}}, 400),
        ({"isBase64Encoded": True}, 400),
        ({"rawPath": "/v1/account/other"}, 404),
        ({"routeKey": "POST /v1/account"}, 404),
        ({"version": "1.0"}, 400),
    ],
)
def test_query_accepts_only_exact_readonly_request(config, change, status):
    event = {**query_event(config), **change}
    factory = Mock()
    result = AccountQueryHandler(config, reader_factory=factory)(event)
    assert result["statusCode"] == status
    assert SENTINEL not in json.dumps(result)
    factory.assert_not_called()


def test_missing_account_does_not_provision_or_infer_setup(config):
    dynamo = DynamoFake()
    result = AccountQueryHandler(
        config, reader_factory=lambda: DynamoAccountReader(dynamo, config)
    )(
        query_event(config),
    )
    assert result["statusCode"] == 404
    assert dynamo.puts == []


@pytest.mark.parametrize(
    "damage",
    [
        "foreign_owner",
        "foreign_client",
        "wrong_key",
        "wrong_entity",
        "extra_envelope",
        "extra_payload",
        "entitlement",
        "ready",
        "bool_version",
        "duplicate_json",
    ],
)
def test_corrupt_or_foreign_record_fails_closed_without_private_values(config, damage):
    dynamo = DynamoFake()
    original = account(config)
    row = envelope(original)
    data = original.payload()
    if damage == "foreign_owner":
        row = envelope(account(config, OTHER_SUBJECT))
    elif damage == "foreign_client":
        data["client_id"] = "otherclient"
    elif damage == "wrong_key":
        row["PK"] = {"S": "OWNER#" + "a" * 64}
    elif damage == "wrong_entity":
        row["entity_type"] = {"S": "LEGACY_OWNER"}
    elif damage == "extra_envelope":
        row["secret"] = {"S": SENTINEL}
    elif damage == "extra_payload":
        data["secret_arn"] = SENTINEL
    elif damage == "entitlement":
        data["entitlements"] = ["seller"]
    elif damage == "ready":
        data["setup_state"] = "ready"
    elif damage == "bool_version":
        data["record_version"] = True
    elif damage == "duplicate_json":
        row["payload"] = {
            "S": '{"owner_id":"' + original.owner_id + '","owner_id":"' + SENTINEL + '"}'
        }
    if damage in {"foreign_client", "extra_payload", "entitlement", "ready", "bool_version"}:
        row["payload"] = {"S": json.dumps(data)}
    dynamo.items[account_key(original.owner_id)] = row
    result = AccountQueryHandler(
        config, reader_factory=lambda: DynamoAccountReader(dynamo, config)
    )(
        query_event(config),
    )
    assert result["statusCode"] == 503
    assert SENTINEL not in json.dumps(result)
    assert original.owner_id not in result["body"]
    assert dynamo.puts == []


def test_query_revalidates_reader_identity_even_if_adapter_is_replaced(config):
    reader = Mock()
    reader.get.return_value = account(config, OTHER_SUBJECT)
    result = AccountQueryHandler(config, reader_factory=lambda: reader)(query_event(config))
    assert result["statusCode"] == 503


@pytest.mark.parametrize(
    "reserved", [[], ["0" * 64], ["A" * 64], ["f" * 64, "f" * 64], [True], {}, "not-json"]
)
def test_reserved_owner_configuration_is_mandatory_nonempty_unique_and_exact(config, reserved):
    values = environment(config)
    values[PREFIX + "RESERVED_OWNER_IDS"] = json.dumps(reserved)
    with pytest.raises(ValueError, match="Invalid account configuration"):
        AccountConfig.from_environment(values)


@pytest.mark.parametrize(
    "missing", ["USER_POOL_ID", "CLIENT_ID", "TABLE_NAME", "RESERVED_OWNER_IDS"]
)
def test_enabled_configuration_requires_every_field_without_echoing_values(config, missing):
    values = environment(config)
    del values[PREFIX + missing]
    with pytest.raises(ValueError) as error:
        AccountConfig.from_environment(values)
    assert str(error.value) == "Invalid account configuration"
    assert error.value.__context__ is None


def test_configuration_derives_exact_authority_and_ignores_group_overrides(config):
    values = {**environment(config), PREFIX + "GROUP": "seller", PREFIX + "SCOPE": "admin"}
    assert AccountConfig.from_environment(values) == config
    assert config.issuer == "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_ExamplePool"


@pytest.mark.parametrize("flag", [None, "false", "TRUE", "1"])
def test_disabled_entrypoints_are_inert_without_configuration_or_dependencies(monkeypatch, flag):
    for key in list(__import__("os").environ):
        if key.startswith(PREFIX):
            monkeypatch.delenv(key)
    if flag is not None:
        monkeypatch.setenv(PREFIX + "PROVISION_ENABLED", flag)
        monkeypatch.setenv(PREFIX + "QUERY_ENABLED", flag)
    build_provision = Mock(side_effect=AssertionError("must remain inert"))
    build_query = Mock(side_effect=AssertionError("must remain inert"))
    monkeypatch.setattr(provision_entrypoint, "build_provisioner", build_provision)
    monkeypatch.setattr(query_entrypoint, "build_reader", build_query)
    event = {"private": SENTINEL}
    assert provision_entrypoint.lambda_handler(event) is event
    query_result = query_entrypoint.lambda_handler(event)
    assert query_result["statusCode"] == 503
    assert SENTINEL not in json.dumps(query_result)
    build_provision.assert_not_called()
    build_query.assert_not_called()


def test_enabled_provision_entrypoint_validates_before_dependency_and_returns_same_event(
    config, monkeypatch
):
    for key, value in environment(config).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(PREFIX + "PROVISION_ENABLED", "true")
    dynamo, cognito = DynamoFake(), CognitoFake()
    factory = Mock(return_value=provisioner(config, dynamo, cognito))
    monkeypatch.setattr(provision_entrypoint, "build_provisioner", factory)
    event = confirmation(config)
    assert provision_entrypoint.lambda_handler(event) is event
    assert factory.call_count == 1
    event["triggerSource"] = "PostConfirmation_ConfirmForgotPassword"
    assert provision_entrypoint.lambda_handler(event) is event
    event["triggerSource"] = "PostConfirmation_ConfirmSignUp"
    event["request"]["userAttributes"]["identities"] = "[]"
    assert provision_entrypoint.lambda_handler(event) is event
    assert factory.call_count == 1
    del event["request"]["userAttributes"]["identities"]
    event["callerContext"]["clientId"] = SENTINEL
    with pytest.raises(AccountUnavailable) as error:
        provision_entrypoint.lambda_handler(event)
    assert SENTINEL not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None
    assert factory.call_count == 1


def test_query_entrypoint_authenticates_before_aws_client_construction(config, monkeypatch):
    for key, value in environment(config).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(PREFIX + "QUERY_ENABLED", "true")
    factory = Mock(side_effect=RuntimeError(SENTINEL))
    monkeypatch.setattr(query_entrypoint, "build_reader", factory)
    event = query_event(config)
    del event["requestContext"]["authorizer"]
    assert query_entrypoint.lambda_handler(event)["statusCode"] == 401
    factory.assert_not_called()
    result = query_entrypoint.lambda_handler(query_event(config))
    assert result["statusCode"] == 503
    assert SENTINEL not in json.dumps(result)
    assert factory.call_count == 1


def test_failures_do_not_log_trigger_attributes_or_credential_sentinels(config, caplog, capsys):
    db, cognito = Mock(), CognitoFake()
    db.put_item.side_effect = RuntimeError(SENTINEL)
    signup = native_signup(confirmation(config), config)
    with pytest.raises(AccountUnavailable) as error:
        provisioner(config, db, cognito).provision(signup)
    assert error.value.__context__ is None
    assert SENTINEL not in "".join(traceback.format_exception(error.value))
    assert cognito.calls == []
    assert SENTINEL not in caplog.text
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_private_account_identity_does_not_accept_unsafe_or_forged_subject(config):
    with pytest.raises(ValueError):
        AccountIdentity("a" * 64, config.issuer, SUBJECT, config.client_id, "b" * 64)
    with pytest.raises(ValueError):
        MerchantAccount(account(config).identity, True)


def test_runtime_factories_construct_only_their_separate_narrow_clients(config, monkeypatch):
    import boto3

    session = Mock()
    session.client.return_value = Mock()
    make_session = Mock(return_value=session)
    monkeypatch.setattr(boto3, "Session", make_session)
    query_entrypoint.build_reader.cache_clear()
    provision_entrypoint.build_provisioner.cache_clear()
    try:
        reader = query_entrypoint.build_reader(config)
        assert not hasattr(reader, "create_if_absent")
        assert [call.args[0] for call in session.client.call_args_list] == ["dynamodb"]
        make_session.assert_called_once_with(region_name=config.region)
        options = session.client.call_args.kwargs["config"]
        assert options.connect_timeout == 3
        assert options.read_timeout == 3
        assert options.retries == {"total_max_attempts": 1}
        session.client.reset_mock()
        provision_entrypoint.build_provisioner(config)
        assert [call.args[0] for call in session.client.call_args_list] == [
            "dynamodb",
            "cognito-idp",
        ]
        # Factory construction does not perform any account or group operation.
        assert session.client.return_value.method_calls == []
    finally:
        query_entrypoint.build_reader.cache_clear()
        provision_entrypoint.build_provisioner.cache_clear()


def test_unknown_record_write_outcome_cannot_grant_a_group_before_authoritative_retry(config):
    dynamo, cognito = DynamoFake(), CognitoFake()
    dynamo.lose_next_put_response = True
    signup = native_signup(confirmation(config), config)
    with pytest.raises(AccountUnavailable):
        provisioner(config, dynamo, cognito).provision(signup)
    assert len(dynamo.items) == 1
    assert cognito.calls == []
    provisioner(config, dynamo, cognito, now=NOW + 1).provision(signup)
    assert len(cognito.calls) == 1
    assert len(dynamo.items) == 1


@pytest.mark.parametrize(
    "failure", ["missing_record", "record_unknown", "group_failure", "group_unknown"]
)
def test_native_pre_authentication_recovers_confirmation_partial_outcome_before_return(
    config, monkeypatch, failure, caplog, capsys
):
    dynamo, cognito = DynamoFake(), CognitoFake()
    original_group = cognito.admin_add_user_to_group
    if failure == "record_unknown":
        dynamo.lose_next_put_response = True
    if failure == "group_unknown":
        cognito.lose_next_response = True
    if failure == "group_failure":
        cognito.admin_add_user_to_group = Mock(side_effect=RuntimeError(SENTINEL))
    service = provisioner(config, dynamo, cognito)
    for key, value in environment(config).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(PREFIX + "PROVISION_ENABLED", "true")
    monkeypatch.setattr(provision_entrypoint, "build_provisioner", Mock(return_value=service))
    if failure != "missing_record":
        with pytest.raises(AccountUnavailable) as error:
            provision_entrypoint.lambda_handler(confirmation(config))
        assert error.value.__context__ is None
        assert SENTINEL not in "".join(traceback.format_exception(error.value))
    before = copy.deepcopy(dynamo.items)
    cognito.admin_add_user_to_group = original_group
    event = pre_authentication(config)
    event["request"]["validationData"] = {
        "owner_id": "a" * 64,
        "group": "seller",
        "token": SENTINEL,
    }
    original_event = copy.deepcopy(event)
    assert provision_entrypoint.lambda_handler(event) is event
    assert event == original_event
    if before:
        assert dynamo.items == before
    else:
        assert len(dynamo.items) == 1
    assert cognito.memberships == {(config.user_pool_id, SUBJECT, "account")}
    after = copy.deepcopy(dynamo.items)
    assert provision_entrypoint.lambda_handler(event) is event
    assert dynamo.items == after
    assert all(call["GroupName"] == "account" for call in cognito.calls)
    assert SENTINEL not in repr(dynamo.items)
    output = capsys.readouterr()
    assert SENTINEL not in caplog.text + output.out + output.err
    assert cognito.reads == [{"UserPoolId": config.user_pool_id, "Username": SUBJECT}] * 2


@pytest.mark.parametrize("excluded", ["reserved", "external", "linked"])
def test_pre_auth_reserved_owner_and_federated_users_keep_login_without_dependency(
    config, monkeypatch, excluded
):
    if excluded == "reserved":
        config = AccountConfig(
            config.user_pool_id,
            config.client_id,
            config.table_name,
            frozenset({owner_id_for(config.issuer, SUBJECT)}),
        )
    event = pre_authentication(config)
    if excluded == "external":
        event["request"]["userAttributes"]["cognito:user_status"] = "EXTERNAL_PROVIDER"
        event["userName"] = "MrListerJudge_" + SUBJECT
    if excluded == "linked":
        event["request"]["userAttributes"]["identities"] = '[{"providerName":"MrListerJudge"}]'
    for key, value in environment(config).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(PREFIX + "PROVISION_ENABLED", "true")
    factory = Mock(side_effect=AssertionError("excluded identity must not build dependencies"))
    monkeypatch.setattr(provision_entrypoint, "build_provisioner", factory)
    assert provision_entrypoint.lambda_handler(event) is event
    factory.assert_not_called()
    # Exclusion is not a way around the exact trusted trigger authority.
    event["callerContext"]["clientId"] = "otherclient"
    with pytest.raises(AccountUnavailable):
        provision_entrypoint.lambda_handler(event)
    factory.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "boolean_string",
        "boolean_integer",
        "boolean_null",
        "status_unconfirmed",
        "status_password_reset",
        "unverified",
        "wrong_pool",
        "wrong_subject",
    ],
)
def test_pre_authentication_invalid_or_unconfirmed_user_fails_before_dependencies(
    config, monkeypatch, mutation
):
    event = pre_authentication(config)
    request = event["request"]
    attributes = request["userAttributes"]
    if mutation.startswith("boolean") or mutation == "unknown":
        request["userNotFound"] = {
            "unknown": True,
            "boolean_string": "false",
            "boolean_integer": 0,
            "boolean_null": None,
        }[mutation]
    if mutation == "status_unconfirmed":
        attributes["cognito:user_status"] = "UNCONFIRMED"
    if mutation == "status_password_reset":
        attributes["cognito:user_status"] = "RESET_REQUIRED"
    if mutation == "unverified":
        attributes["email_verified"] = "false"
    if mutation == "wrong_pool":
        event["userPoolId"] = "us-west-2_Other"
    if mutation == "wrong_subject":
        attributes["sub"] = OTHER_SUBJECT
    for key, value in environment(config).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(PREFIX + "PROVISION_ENABLED", "true")
    factory = Mock(side_effect=AssertionError("invalid event must not build dependencies"))
    monkeypatch.setattr(provision_entrypoint, "build_provisioner", factory)
    with pytest.raises(AccountUnavailable) as error:
        provision_entrypoint.lambda_handler(event)
    assert error.value.__context__ is None
    factory.assert_not_called()


def test_optional_documented_user_not_found_field_can_be_absent(config):
    event = pre_authentication(config)
    event["request"].pop("userNotFound")
    assert (
        native_signup(event, config).identity
        == native_signup(confirmation(config), config).identity
    )


def test_disabled_pre_authentication_is_unchanged_without_constructing_dependencies(monkeypatch):
    monkeypatch.delenv(PREFIX + "PROVISION_ENABLED", raising=False)
    factory = Mock(side_effect=AssertionError("disabled"))
    monkeypatch.setattr(provision_entrypoint, "build_provisioner", factory)
    event = {
        "triggerSource": PRE_AUTHENTICATION,
        "request": {"validationData": {"secret": SENTINEL}},
    }
    assert provision_entrypoint.lambda_handler(event) is event
    factory.assert_not_called()


@pytest.mark.parametrize("event_status", ["missing", "confirmed"])
def test_pre_authentication_uses_authoritative_profile_even_when_event_status_absent(
    config, event_status
):
    event = pre_authentication(config)
    if event_status == "missing":
        event["request"]["userAttributes"].pop("cognito:user_status")
    dynamo, cognito = DynamoFake(), CognitoFake()
    result = provisioner(config, dynamo, cognito).provision(native_signup(event, config))
    assert result.owner_id == owner_id_for(config.issuer, SUBJECT)
    assert cognito.reads == [{"UserPoolId": config.user_pool_id, "Username": SUBJECT}]
    assert len(dynamo.items) == len(cognito.memberships) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "unconfirmed",
        "disabled",
        "foreign_subject",
        "foreign_username",
        "changed_email",
        "unverified",
        "linked",
        "duplicate",
        "missing_attributes",
        "sdk_failure",
    ],
)
def test_pre_auth_profile_mismatch_or_unknown_read_never_writes_or_grants(
    config, mutation, capsys, caplog
):
    event = pre_authentication(config)
    event["request"]["userAttributes"].pop("cognito:user_status")
    signup = native_signup(event, config)
    dynamo, cognito = DynamoFake(), CognitoFake()
    response = cognito.admin_get_user(UserPoolId=config.user_pool_id, Username=SUBJECT)
    cognito.reads.clear()
    if mutation == "unconfirmed":
        response["UserStatus"] = "UNCONFIRMED"
    if mutation == "disabled":
        response["Enabled"] = False
    if mutation == "foreign_subject":
        response["UserAttributes"][0]["Value"] = OTHER_SUBJECT
    if mutation == "foreign_username":
        response["Username"] = OTHER_SUBJECT
    if mutation == "changed_email":
        response["UserAttributes"][1]["Value"] = SENTINEL
    if mutation == "unverified":
        response["UserAttributes"][2]["Value"] = "false"
    if mutation == "linked":
        response["UserAttributes"].append({"Name": "identities", "Value": "[]"})
    if mutation == "duplicate":
        response["UserAttributes"].append({"Name": "sub", "Value": OTHER_SUBJECT})
    if mutation == "missing_attributes":
        response.pop("UserAttributes")
    cognito.admin_get_user = Mock(return_value=response)
    if mutation == "sdk_failure":
        cognito.admin_get_user.side_effect = RuntimeError(SENTINEL)
    with pytest.raises(AccountUnavailable) as error:
        provisioner(config, dynamo, cognito).provision(signup)
    cognito.admin_get_user.assert_called_once_with(UserPoolId=config.user_pool_id, Username=SUBJECT)
    assert dynamo.puts == dynamo.gets == cognito.calls == []
    assert error.value.__context__ is None
    assert SENTINEL not in "".join(traceback.format_exception(error.value))
    output = capsys.readouterr()
    assert output.out == output.err == caplog.text == ""


def test_confirmation_does_not_perform_pre_auth_admin_read(config):
    dynamo, cognito = DynamoFake(), CognitoFake()
    cognito.admin_get_user = Mock(
        side_effect=AssertionError("confirmation has direct trusted proof")
    )
    provisioner(config, dynamo, cognito).provision(native_signup(confirmation(config), config))
    cognito.admin_get_user.assert_not_called()


@pytest.mark.parametrize(
    "field,value", [("userName", None), ("userName", "bad\nname"), ("sub", "not-a-subject")]
)
def test_malformed_pre_auth_identity_cannot_take_federated_noop_branch(config, field, value):
    event = pre_authentication(config)
    event["request"]["userAttributes"]["identities"] = "[]"
    if field == "sub":
        event["request"]["userAttributes"][field] = value
    else:
        event[field] = value
    with pytest.raises(ValueError):
        native_signup(event, config)


def test_reserved_pre_auth_keeps_legacy_login_without_imposing_new_email_rules(config):
    protected = AccountConfig(
        config.user_pool_id,
        config.client_id,
        config.table_name,
        frozenset({owner_id_for(config.issuer, SUBJECT)}),
    )
    event = pre_authentication(protected)
    event["userName"] = "existing-owner-name"
    event["request"]["userAttributes"].pop("email")
    event["request"]["userAttributes"].pop("email_verified")
    assert native_signup(event, protected) is None


@pytest.mark.parametrize("event_factory", [confirmation, pre_authentication])
def test_email_alias_event_never_redirects_the_fixed_group_write(config, event_factory):
    event = event_factory(config)
    event["userName"] = event["request"]["userAttributes"]["email"]
    dynamo, cognito = DynamoFake(), CognitoFake()
    result = provisioner(config, dynamo, cognito).provision(native_signup(event, config))
    assert result.identity.username_digest == sha256(event["userName"].encode()).hexdigest()
    assert cognito.calls == [
        {"UserPoolId": config.user_pool_id, "Username": SUBJECT, "GroupName": "account"}
    ]
    if event_factory is pre_authentication:
        assert cognito.reads == [{"UserPoolId": config.user_pool_id, "Username": SUBJECT}]
