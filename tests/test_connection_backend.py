from __future__ import annotations

import copy
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from mr_lister.accounts.models import AccountConfig, MerchantAccount
from mr_lister.accounts.provision import native_signup
from mr_lister.connections import entrypoint
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.connections.credentials import (
    CredentialEnvelope,
    ExactConnectionResolver,
    SecretsManagerCredentialStore,
)
from mr_lister.connections.http import ConnectionHttpHandler
from mr_lister.connections.models import (
    ConnectionConfig,
    ConnectionConflict,
    ConnectionError,
    ConnectionRateLimited,
    ExpiredCandidate,
    InvalidConnectionRequest,
    InvalidCredential,
    Shop,
    ValidationBudget,
    operation_id,
)
from mr_lister.connections.service import ConnectionService, SellerGroupWriter, StoreSetupReader
from mr_lister.connections.store import (
    DynamoConnectionStore,
    budget_key,
    connection_key,
    setup_key,
)
from mr_lister.connections.transport import HOST, PATH, PrintifyShopsTransport

TOKEN = "private-token-sentinel-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
AUTHORIZATION = {
    "accepted": True,
    "terms_version": "2026-10-05",
    "privacy_version": "2026-10-05",
}
NOW = 1800000000
SUBJECT = "11111111-2222-4333-8444-555555555555"
OTHER = "22222222-3333-4444-8555-666666666666"


def merchant(config, subject=SUBJECT):
    event = {
        "version": "1",
        "triggerSource": "PostConfirmation_ConfirmSignUp",
        "region": config.accounts.region,
        "userPoolId": config.accounts.user_pool_id,
        "userName": subject,
        "callerContext": {"clientId": config.accounts.client_id},
        "request": {
            "userAttributes": {
                "sub": subject,
                "email": "native@example.invalid",
                "email_verified": "true",
            }
        },
        "response": {},
    }
    return MerchantAccount(identity=native_signup(event, config.accounts).identity, created_at=NOW)


class DynamoFake:
    def __init__(self):
        self.items = {}
        self.transactions = []
        self.lose_next = False

    def get_item(self, **kw):
        assert kw["ConsistentRead"] is True
        key = kw["Key"]["PK"]["S"]
        return {"Item": copy.deepcopy(self.items[key])} if key in self.items else {}

    def query(self, **kw):
        assert kw["IndexName"] == "CandidateCleanupDue" and kw["Limit"] == 10
        due = int(kw["ExpressionAttributeValues"][":due"]["N"])
        items = [
            item
            for item in self.items.values()
            if "cleanup_due" in item and int(item["cleanup_due"]["N"]) <= due
        ]
        return {"Items": [{"PK": item["PK"]} for item in items[:10]]}

    def transact_write_items(self, **kw):
        actions = kw["TransactItems"]
        self.transactions.append(copy.deepcopy(actions))
        for action in actions:
            data = action.get("Put") or action["ConditionCheck"]
            key = (data.get("Item") or data["Key"])["PK"]["S"]
            current = self.items.get(key)
            expression = data["ConditionExpression"]
            if expression == "attribute_not_exists(PK)":
                valid = current is None
            else:
                valid = current is not None
                for clause in expression.split(" AND "):
                    left, right = clause.split(" = ")
                    valid = (
                        valid
                        and current.get(data["ExpressionAttributeNames"][left])
                        == data["ExpressionAttributeValues"][right]
                    )
            if not valid:
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException", "Message": TOKEN}},
                    "TransactWriteItems",
                )
        for action in actions:
            if "Put" not in action:
                continue
            item = action["Put"]["Item"]
            self.items[item["PK"]["S"]] = copy.deepcopy(item)
        if self.lose_next:
            self.lose_next = False
            raise RuntimeError(TOKEN)
        return {}


class SecretsFake:
    def __init__(self):
        self.items = {}
        self.creates = 0
        self.reads = 0
        self.lose_next = False

    def create_secret(self, **kw):
        self.creates += 1
        name, version = kw["Name"], kw["ClientRequestToken"]
        if name in self.items:
            raise RuntimeError(TOKEN)
        self.items[name] = {"Name": name, "VersionId": version, "SecretString": kw["SecretString"]}
        if self.lose_next:
            self.lose_next = False
            raise RuntimeError(TOKEN)
        return {"Name": name, "VersionId": version}

    def get_secret_value(self, **kw):
        self.reads += 1
        data = self.items[kw["SecretId"]]
        assert data["VersionId"] == kw["VersionId"]
        return dict(data)


class ProviderFake:
    def __init__(self):
        self.calls = 0
        self.stores = (
            Shop(id=7, name="Etsy One", sales_channel="etsy"),
            Shop(id=8, name="Other", sales_channel="shopify"),
        )

    def list_shops(self, token):
        assert isinstance(token, SecretStr)
        assert token.get_secret_value() == TOKEN
        self.calls += 1
        return self.stores


class GroupsFake:
    def __init__(self):
        self.calls = []
        self.fail = False

    def grant(self, account):
        self.calls.append(account.identity.subject)
        if self.fail:
            raise ConnectionError


@pytest.fixture
def system():
    config = ConnectionConfig(
        AccountConfig("us-west-2_TestPool", "testclient", "accounts-test", frozenset({"f" * 64})),
        "connections-test",
        "mr-lister/test/connections/",
        True,
    )
    dynamo, secrets, provider, groups = DynamoFake(), SecretsFake(), ProviderFake(), GroupsFake()
    store = DynamoConnectionStore(dynamo, config)
    credentials = SecretsManagerCredentialStore(secrets, config)
    clock = [NOW]
    service = ConnectionService(
        config=config,
        store=store,
        credentials=credentials,
        provider=provider,
        groups=groups,
        clock=lambda: clock[0],
    )
    return service, merchant(config), dynamo, secrets, provider, groups, clock


def validate(system, account=None, key="validation-key-0001"):
    service, owner, *_ = system
    return service.validate(account or owner, token=TOKEN, idempotency_key=key)


def select(system, candidate, account=None, key="selection-key-0001", shop=7):
    service, owner, *_ = system
    return service.select(
        account or owner,
        candidate_id=candidate["candidate_id"],
        shop_id=shop,
        expected_setup_version=candidate["record_version"],
        idempotency_key=key,
    )


def test_real_adapters_validate_select_public_projection_and_fixed_destination(system):
    service, account, dynamo, secrets, provider, groups, clock = system
    assert service.get(account)["state"] == "connection_required"
    candidate = validate(system)
    assert candidate["expires_at"] == "2027-01-15T08:15:00Z"
    assert candidate["record_version"] == 2
    assert candidate["stores"][0] == {
        "id": "7",
        "name": "Etsy One",
        "sales_channel": "etsy",
        "eligible": True,
        "disabled_reason": None,
    }
    assert candidate["stores"][1]["disabled_reason"] == "unsupported_channel"
    assert service.get(account)["state"] == "choose_store"
    ready = select(system, candidate)
    assert ready["state"] == "ready"
    assert ready["record_version"] == 3
    assert ready["store"]["shop_id"] == 7
    assert groups.calls == [SUBJECT]
    assert provider.calls == 2
    binding = service.store.get_active_binding(
        account.owner_id, ready["store"]["shop_binding_id"], 3
    )
    credential = ExactConnectionResolver(
        directory=service.store, credentials=service.credentials
    ).resolve_exact(binding=binding)
    assert credential.store_binding == binding and credential.shop_id == 7
    assert TOKEN not in repr(credential)
    assert TOKEN not in json.dumps(dynamo.items)
    assert TOKEN not in json.dumps(ready)
    assert TOKEN not in repr(
        CredentialEnvelope.model_validate_json(next(iter(secrets.items.values()))["SecretString"])
    )


def test_two_owners_same_external_shop_have_separate_connection_authority(system):
    service, account, *_ = system
    other = merchant(service.config, OTHER)
    a, b = validate(system), validate(system, other)
    assert a["candidate_id"] != b["candidate_id"]
    with pytest.raises(ConnectionConflict):
        select(system, a, other)
    ready_a, ready_b = select(system, a), select(system, b, other)
    assert ready_a["store"]["connection_id"] != ready_b["store"]["connection_id"]
    with pytest.raises(ConnectionConflict):
        service.store.get_active_binding(other.owner_id, ready_a["store"]["shop_binding_id"], 3)


def test_validation_replay_owned_key_exact_payload_and_no_provider_retry(system):
    service, account, _, _, provider, *_ = system
    original = validate(system)
    assert validate(system) == original and provider.calls == 1
    with pytest.raises(ConnectionConflict):
        service.validate(account, token=TOKEN + "changed", idempotency_key="validation-key-0001")
    assert provider.calls == 1


def test_validation_preserves_working_connection_and_disallows_retarget(system):
    service, account, _, secrets, provider, *_ = system
    ready = select(system, validate(system))
    creates, reads, calls = secrets.creates, secrets.reads, provider.calls
    with pytest.raises(ConnectionConflict):
        validate(system, key="validation-key-0002")
    assert service.get(account) == ready
    assert (secrets.creates, secrets.reads, provider.calls) == (creates, reads, calls)


@pytest.mark.parametrize("shop", [8, 999, True, "7", 0])
def test_selection_rejects_fabricated_unsupported_or_untyped_shop_before_provider(system, shop):
    provider = system[4]
    candidate = validate(system)
    with pytest.raises(InvalidConnectionRequest):
        select(system, candidate, shop=shop)
    assert provider.calls == 1


def test_selection_rechecks_current_membership_and_expiry(system):
    candidate = validate(system)
    system[4].stores = ()
    with pytest.raises(ConnectionConflict):
        select(system, candidate)
    system[6][0] += 900
    assert system[0].get(system[1])["state"] == "connection_required"
    with pytest.raises(ExpiredCandidate):
        select(system, candidate)


def test_unknown_record_secret_and_selection_responses_recover_exactly(system):
    system[2].lose_next = True
    system[3].lose_next = True
    candidate = validate(system)
    system[2].lose_next = True
    ready = select(system, candidate)
    assert ready["state"] == "ready"
    assert system[4].calls == 2
    assert select(system, candidate) == ready
    assert system[4].calls == 2


def test_group_failure_survives_reload_and_candidate_expiry_without_secret_or_provider(system):
    service, account, _, secrets, provider, groups, clock = system
    candidate = validate(system)
    groups.fail = True
    with pytest.raises(ConnectionError):
        select(system, candidate)
    pending = service.get(account)
    assert pending["state"] == "reconnect_required" and pending["store"]["shop_id"] == 7
    with pytest.raises(ConnectionConflict):
        service.store.get_active_binding(account.owner_id, pending["store"]["shop_binding_id"], 3)
    clock[0] += 1000
    reads, calls = secrets.reads, provider.calls
    groups.fail = False
    assert service.activate(account)["state"] == "ready"
    assert select(system, candidate)["state"] == "ready"
    assert secrets.reads == reads and provider.calls == calls
    assert groups.calls == [SUBJECT, SUBJECT]


def test_workflow_flag_locks_selected_connection_and_cannot_grant(system):
    service, account, dynamo, secrets, provider, groups, clock = system
    config = replace(service.config, workflow_enabled=False)
    service.config = config
    service.store = service.directory = DynamoConnectionStore(dynamo, config)
    result = select(system, validate(system))
    assert result["state"] == "connection_unavailable" and result["store"] is None
    assert not groups.calls
    with pytest.raises(ConnectionConflict):
        service.activate(account)


def test_secret_owner_connection_binding_version_and_generation_mismatch_fail_closed(system):
    candidate = validate(system)
    record = system[0].store.get_candidate(system[1].owner_id, candidate["candidate_id"])
    raw = next(iter(system[3].items.values()))
    original = raw["SecretString"]
    for field, value in [
        ("owner_id", "e" * 64),
        ("connection_id", "conn_" + "e" * 32),
        ("shop_binding_id", "binding_" + "e" * 32),
        ("generation", 2),
    ]:
        data = json.loads(original)
        data[field] = value
        raw["SecretString"] = json.dumps(data)
        with pytest.raises(ConnectionError) as error:
            system[0].credentials.load(record)
        assert error.value.__context__ is None
        assert TOKEN not in str(error.value)
    raw["SecretString"] = original
    raw["VersionId"] = "e" * 64
    with pytest.raises(ConnectionError):
        system[0].credentials.load(record)


def test_original_connection_epoch_guard_independent_of_mutable_selected_setup(system):
    service, account, dynamo, *_ = system
    ready = select(system, validate(system))
    binding = service.store.get_active_binding(
        account.owner_id, ready["store"]["shop_binding_id"], 3
    )
    dynamo.items.pop(setup_key(account.owner_id))
    assert service.store.require_current(binding).binding == binding
    condition = service.store.current_epoch_condition(binding)["ConditionCheck"]
    assert condition["Key"] == {"PK": {"S": connection_key(binding)}}
    for name in (
        "owner_id",
        "connection_id",
        "shop_binding_id",
        "shop_id",
        "authorization_epoch",
        "binding_fingerprint",
        "status",
    ):
        assert "#" + name in condition["ExpressionAttributeNames"]
    row = dynamo.items[connection_key(binding)]
    row["authorization_epoch"] = {"N": "2"}
    with pytest.raises(ConnectionError):
        service.store.require_current(binding)


def event(config, operation="get", body=None, groups=None, subject=SUBJECT):
    route = {
        "get": "GET /v1/store-setup",
        "validate": "POST /v1/connections/printify/validate",
        "select": "POST /v1/connections/printify/select-shop",
        "activate": "POST /v1/connections/printify/activate",
    }[operation]
    method, path = route.split(" ")
    return {
        "version": "2.0",
        "routeKey": route,
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"idempotency-key": "http-operation-0001"},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "iss": config.accounts.issuer,
                        "client_id": config.accounts.client_id,
                        "sub": subject,
                        "token_use": "access",
                        "scope": "mr-lister-api/seller",
                        "cognito:groups": groups or ["account"],
                    }
                }
            },
        },
    }


@pytest.mark.parametrize("group", [["seller"], ["judge"], ["seller", "judge"]])
def test_judge_legacy_and_unauthenticated_rejected_before_dependencies(system, group):
    service, account, *_ = system
    accounts, factory = Mock(), Mock()
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=factory,
        allowed_operation="get",
    )
    assert handler(event(service.config, groups=group))["statusCode"] == 403
    request = event(service.config)
    del request["requestContext"]["authorizer"]
    assert handler(request)["statusCode"] == 401
    accounts.assert_not_called()
    factory.assert_not_called()


def test_reserved_account_and_foreign_or_malformed_record_cannot_construct_service(system):
    service, account, *_ = system
    reserved = replace(
        service.config,
        accounts=replace(service.config.accounts, reserved_owner_ids=frozenset({account.owner_id})),
    )
    accounts, factory = Mock(), Mock()
    handler = ConnectionHttpHandler(
        config=reserved,
        account_reader_factory=accounts,
        service_factory=factory,
        allowed_operation="get",
    )
    assert handler(event(reserved))["statusCode"] == 403
    accounts.assert_not_called()
    factory.assert_not_called()
    accounts.return_value.get.return_value = merchant(service.config, OTHER)
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=factory,
        allowed_operation="get",
    )
    assert handler(event(service.config))["statusCode"] == 403
    factory.assert_not_called()


def test_http_token_validation_retains_explicit_versioned_merchant_permission(system):
    service, account, dynamo, secrets, provider, *_ = system
    accounts = Mock()
    accounts.return_value.get.return_value = account
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=lambda: service,
        allowed_operation="validate",
    )
    result = handler(
        event(
            service.config,
            "validate",
            {
                "token": TOKEN,
                "authorization": AUTHORIZATION,
            },
        )
    )
    assert result["statusCode"] == 200
    candidate_id = json.loads(result["body"])["candidate_id"]
    candidate = service.store.get_candidate(account.owner_id, candidate_id)
    assert candidate.merchant_authorization_version == AUTHORIZATION["terms_version"]
    assert candidate.created_at == NOW
    assert TOKEN not in result["body"]
    assert secrets.creates == 1 and provider.calls == 1


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        {},
        {**AUTHORIZATION, "accepted": False},
        {**AUTHORIZATION, "accepted": "true"},
        {**AUTHORIZATION, "terms_version": "old"},
        {**AUTHORIZATION, "privacy_version": "old"},
        {**AUTHORIZATION, "owner_id": "e" * 64},
    ],
)
def test_merchant_permission_is_checked_before_account_or_secret_dependencies(
    system, authorization
):
    service, account, *_ = system
    accounts, factory = Mock(), Mock()
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=factory,
        allowed_operation="validate",
    )
    result = handler(
        event(
            service.config,
            "validate",
            {
                "token": TOKEN,
                "authorization": authorization,
            },
        )
    )
    assert result["statusCode"] == 400
    assert TOKEN not in result["body"]
    accounts.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("body", [{"token": TOKEN, "owner_id": "e" * 64}, {}, {"token": None}])
def test_http_strict_body_and_safe_errors_never_echo_token(system, body, caplog, capsys):
    service, account, *_ = system
    accounts = Mock()
    accounts.return_value.get.return_value = account
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=lambda: service,
        allowed_operation="validate",
    )
    result = handler(event(service.config, "validate", body))
    assert result["statusCode"] == 400
    assert TOKEN not in json.dumps(result) + caplog.text + str(capsys.readouterr())


def test_route_scoped_read_only_handler_and_disabled_entrypoints(system, monkeypatch):
    service, account, *_ = system
    accounts = Mock()
    accounts.return_value.get.return_value = account
    reader = StoreSetupReader(config=service.config, directory=service.directory, clock=lambda: NOW)
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=lambda: reader,
        allowed_operation="get",
    )
    assert handler(event(service.config))["statusCode"] == 200
    assert handler(event(service.config, "activate", {}))["statusCode"] == 404
    monkeypatch.delenv("MR_LISTER_CONNECTION_ENABLED", raising=False)
    monkeypatch.setattr(entrypoint, "build_handler", Mock(side_effect=AssertionError))
    for func in (
        entrypoint.query_handler,
        entrypoint.validate_handler,
        entrypoint.select_handler,
        entrypoint.activate_handler,
    ):
        assert func({})["statusCode"] == 503


def test_cognito_grant_uses_native_immutable_subject_and_fixed_group(system):
    service, account, *_ = system
    client = Mock()
    SellerGroupWriter(client, service.config).grant(account)
    client.admin_add_user_to_group.assert_called_once_with(
        UserPoolId=service.config.accounts.user_pool_id, Username=SUBJECT, GroupName="seller"
    )
    client.admin_add_user_to_group.side_effect = RuntimeError(TOKEN)
    with pytest.raises(ConnectionError) as error:
        SellerGroupWriter(client, service.config).grant(account)
    assert error.value.__context__ is None and TOKEN not in str(error.value)


def test_transport_fixed_https_route_no_redirect_and_bounded_response():
    connection = Mock()
    result = connection.getresponse.return_value
    result.status = 200
    result.getheader.return_value = "application/json; charset=utf-8"
    result.read.return_value = b'[{"id":7,"name":"Shop","sales_channel":"etsy"}]'
    factory = Mock(return_value=connection)
    transport = PrintifyShopsTransport(connection_factory=factory)
    assert transport.list_shops(SecretStr(TOKEN))[0].id == 7
    assert factory.call_args.args == (HOST,)
    assert connection.request.call_args.args == ("GET", PATH)
    assert result.read.call_args.args == (65537,)
    assert connection.close.called
    result.status = 302
    with pytest.raises(ConnectionError) as error:
        transport.list_shops(SecretStr(TOKEN))
    assert error.value.__context__ is None
    result.status = 403
    with pytest.raises(InvalidCredential):
        transport.list_shops(SecretStr(TOKEN))


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * 65537,
        b"{}",
        b"[] trailing",
        b'[{"id":7,"id":8}]',
        b'[{"id":true,"name":"X","sales_channel":"etsy"}]',
    ],
)
def test_transport_rejects_unbounded_duplicate_and_malformed_data(raw):
    connection = Mock()
    result = connection.getresponse.return_value
    result.status = 200
    result.getheader.return_value = "application/json"
    result.read.return_value = raw
    with pytest.raises(ConnectionError):
        PrintifyShopsTransport(connection_factory=Mock(return_value=connection)).list_shops(
            SecretStr(TOKEN)
        )


def test_expired_candidate_cleanup_claims_metadata_and_never_deletes_promoted_secret(system):
    from mr_lister.connections.cleanup import CandidateCleanupService, CandidateSecretDisposer

    service, account, _, _, _, _, clock = system
    candidate = validate(system)
    client = Mock()
    record = service.store.get_candidate(account.owner_id, candidate["candidate_id"])
    client.delete_secret.return_value = {"Name": record.secret.name}
    cleanup = CandidateCleanupService(
        store=service.store,
        disposer=CandidateSecretDisposer(client, service.config),
        clock=lambda: clock[0],
    )
    with pytest.raises(ConnectionConflict):
        cleanup.cleanup(owner_id=account.owner_id, candidate_id=candidate["candidate_id"])
    client.delete_secret.assert_not_called()
    clock[0] += 1801
    assert (
        cleanup.cleanup(owner_id=account.owner_id, candidate_id=candidate["candidate_id"])
        == "deleted"
    )
    client.delete_secret.assert_called_once_with(
        SecretId=record.secret.name, RecoveryWindowInDays=7
    )
    assert (
        cleanup.cleanup(owner_id=account.owner_id, candidate_id=candidate["candidate_id"])
        == "deleted"
    )
    assert client.delete_secret.call_count == 1
    fresh = validate(system, key="fresh-validation-01")
    select(system, fresh, key="fresh-selection-001")
    clock[0] += 1801
    assert (
        cleanup.cleanup(owner_id=account.owner_id, candidate_id=fresh["candidate_id"]) == "consumed"
    )
    assert client.delete_secret.call_count == 1


def test_cleanup_delete_failure_retains_retry_metadata_and_blocks_candidate_publication(system):
    from mr_lister.connections.cleanup import CandidateCleanupService, CandidateSecretDisposer

    service, account, _, _, _, _, clock = system
    candidate = validate(system)
    clock[0] += 1801
    client = Mock()
    client.delete_secret.side_effect = RuntimeError(TOKEN)
    cleanup = CandidateCleanupService(
        store=service.store,
        disposer=CandidateSecretDisposer(client, service.config),
        clock=lambda: clock[0],
    )
    with pytest.raises(ConnectionError) as error:
        cleanup.cleanup(owner_id=account.owner_id, candidate_id=candidate["candidate_id"])
    assert error.value.__context__ is None
    current = service.store.get_candidate(account.owner_id, candidate["candidate_id"])
    assert current.state == "deleting" and current.secret is not None
    assert service.get(account)["state"] == "connection_required"


def test_query_and_activation_build_no_secret_or_provider_clients(system, monkeypatch):
    service, account, dynamo, _, provider, groups, _ = system
    candidate = validate(system)
    groups.fail = True
    with pytest.raises(ConnectionError):
        select(system, candidate)
    clients = []
    cognito = Mock()

    def sdk(kind, region):
        clients.append(kind)
        if kind == "dynamodb":
            return dynamo
        if kind == "cognito-idp":
            return cognito
        raise AssertionError("Unexpected credential capability")

    monkeypatch.setattr(entrypoint, "sdk_client", sdk)
    monkeypatch.setattr(entrypoint, "DynamoAccountReader", lambda *_: Mock(get=lambda _: account))
    monkeypatch.setattr(entrypoint, "PrintifyShopsTransport", Mock(side_effect=AssertionError))
    query = entrypoint.build_handler(service.config, "get")
    assert query(event(service.config))["statusCode"] == 200
    assert set(clients) == {"dynamodb"}
    clients.clear()
    activation = entrypoint.build_handler(service.config, "activate")
    response = activation(event(service.config, "activate", {}))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["state"] == "ready"
    assert set(clients) == {"dynamodb", "cognito-idp"}
    assert provider.calls == 2


@pytest.mark.parametrize(
    "change", ["issuer", "client", "idtoken", "scope", "query", "base64", "extra", "duplicate"]
)
def test_http_malformed_claims_or_requests_never_construct_service(system, change):
    service, account, *_ = system
    accounts, factory = Mock(), Mock()
    accounts.return_value.get.return_value = account
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=factory,
        allowed_operation="validate",
    )
    request = event(service.config, "validate", {"token": TOKEN, "authorization": AUTHORIZATION})
    claims = request["requestContext"]["authorizer"]["jwt"]["claims"]
    if change == "issuer":
        claims["iss"] += "/other"
    if change == "client":
        claims["client_id"] = "otherclient"
    if change == "idtoken":
        claims["token_use"] = "id"
    if change == "scope":
        claims["scope"] = "openid"
    if change == "query":
        request["rawQueryString"] = "owner=other"
    if change == "base64":
        request["isBase64Encoded"] = True
    if change == "extra":
        request["body"] = json.dumps(
            {"token": TOKEN, "authorization": AUTHORIZATION, "url": "https://evil.invalid"}
        )
    if change == "duplicate":
        request["body"] = '{"token":"first","token":"second"}'
    result = handler(request)
    assert result["statusCode"] in (400, 403)
    factory.assert_not_called()
    assert TOKEN not in result["body"]


def test_epoch_change_same_external_shop_and_foreign_internal_id_cannot_resolve(system):
    service, account, *_ = system
    ready = select(system, validate(system))
    binding = service.store.get_active_binding(
        account.owner_id, ready["store"]["shop_binding_id"], 3
    )
    resolver = ExactConnectionResolver(directory=service.store, credentials=service.credentials)
    for field, value in [
        ("authorization_epoch", 2),
        ("connection_id", "conn_" + "e" * 32),
        ("shop_binding_id", "binding_" + "e" * 32),
    ]:
        values = binding.model_dump()
        values[field] = value
        changed = StoreBindingAuthority.create(
            **{
                key: values[key]
                for key in (
                    "owner_id",
                    "connection_id",
                    "shop_binding_id",
                    "shop_id",
                    "authorization_epoch",
                )
            }
        )
        with pytest.raises(ConnectionError) as error:
            resolver.resolve_exact(binding=changed)
        assert error.value.__context__ is None


def test_selection_transaction_has_all_four_conditional_writes_and_marker_fences_epoch(system):
    validate_result = validate(system)
    select(system, validate_result)
    transactions = system[2].transactions
    selection = next(actions for actions in transactions if len(actions) == 4)
    assert all("ConditionExpression" in action["Put"] for action in selection)
    marker = transactions[-1]
    assert len(marker) == 2
    connection_action = next(
        action["Put"]
        for action in marker
        if action["Put"]["Item"]["entity_type"] == {"S": "Connection"}
    )
    for field in (
        "status",
        "authorization_epoch",
        "connection_id",
        "shop_binding_id",
        "shop_id",
        "owner_id",
        "binding_fingerprint",
    ):
        assert "#" + field in connection_action["ExpressionAttributeNames"]


def test_cleanup_index_is_removed_atomically_on_selection_and_due_query_is_bounded(system):
    from mr_lister.connections.cleanup import CandidateCleanupService, CandidateSecretDisposer

    service, account, dynamo, _, _, _, clock = system
    orphan = validate(system)
    selected = validate(system, key="later-validation-01")
    select(system, selected)
    record = service.store.get_candidate(account.owner_id, orphan["candidate_id"])
    client = Mock()
    client.delete_secret.return_value = {"Name": record.secret.name}
    clock[0] += 1801
    cleanup = CandidateCleanupService(
        store=service.store,
        disposer=CandidateSecretDisposer(client, service.config),
        clock=lambda: clock[0],
    )
    assert cleanup.run_due(index_name="CandidateCleanupDue") == {
        "processed": 1,
        "completed": 1,
        "failed": 0,
    }
    assert client.delete_secret.call_count == 1
    assert cleanup.run_due(index_name="CandidateCleanupDue")["processed"] == 0
    with pytest.raises(ConnectionError):
        cleanup.run_due(index_name="WrongIndex")
    for row in dynamo.items.values():
        if row["entity_type"] == {"S": "Candidate"}:
            assert "cleanup_due" not in row and "cleanup_partition" not in row


def budget(system):
    owner, dynamo = system[1], system[2]
    row = dynamo.items[budget_key(owner.owner_id)]
    return ValidationBudget.model_validate_json(row["payload"]["S"])


def test_validation_budget_caps_secret_creation_even_when_provider_rejects_every_token(system):
    service, account, dynamo, secrets, provider, *_ = system
    provider.list_shops = Mock(side_effect=InvalidCredential)
    for index in range(5):
        with pytest.raises(InvalidCredential):
            validate(system, key=f"invalid-attempt-{index:03d}")
    with pytest.raises(ConnectionRateLimited) as error:
        validate(system, key="invalid-attempt-006")
    assert error.value.status == 429 and error.value.__context__ is None
    assert secrets.creates == provider.list_shops.call_count == 5
    assert len(budget(system).attempted_at) == 5
    assert service.store.get_setup(account.owner_id).candidate_id is None
    assert TOKEN not in json.dumps(dynamo.items) + str(error.value)
    rows = [row for row in dynamo.items.values() if row["entity_type"] == {"S": "Candidate"}]
    assert len(rows) == 5
    reservation = dynamo.transactions[0]
    assert len(reservation) == 3
    assert reservation[1]["Put"]["Item"]["entity_type"] == {"S": "ValidationBudget"}
    assert "ConditionCheck" in reservation[2]


def test_budget_replays_and_lost_reservation_response_never_charge_twice(system):
    service, account, dynamo, secrets, provider, *_ = system
    dynamo.lose_next = True
    first = validate(system)
    assert len(budget(system).attempted_at) == 1
    assert validate(system) == first
    assert len(budget(system).attempted_at) == 1
    assert secrets.creates == provider.calls == 1
    with pytest.raises(ConnectionConflict):
        service.validate(account, token=TOKEN + "changed", idempotency_key="validation-key-0001")
    assert len(budget(system).attempted_at) == 1


def test_budget_counts_failed_secret_attempts_and_allows_new_token_retries_within_limit(system):
    service, account, _, secrets, provider, *_ = system
    original = service.credentials.save
    service.credentials.save = Mock(side_effect=ConnectionError)
    for index in range(2):
        with pytest.raises(ConnectionError):
            validate(system, key=f"failed-secret-{index:04d}")
    assert len(budget(system).attempted_at) == 2
    service.credentials.save = original
    candidate = validate(system, key="revised-token-attempt")
    assert candidate["stores"][0]["id"] == "7"
    assert len(budget(system).attempted_at) == 3
    assert secrets.creates == provider.calls == 1


def test_budget_is_sliding_window_and_isolated_per_owner(system):
    service, account, _, secrets, provider, _, clock = system
    for index in range(5):
        validate(system, key=f"window-attempt-{index:03d}")
        clock[0] += 1
    with pytest.raises(ConnectionRateLimited):
        validate(system, key="window-attempt-full")
    other = merchant(service.config, OTHER)
    validate(system, account=other, key="window-attempt-full")
    clock[0] = NOW + 900
    validate(system, key="window-attempt-renew")
    assert budget(system).attempted_at == (NOW + 1, NOW + 2, NOW + 3, NOW + 4, NOW + 900)
    with pytest.raises(ConnectionRateLimited):
        validate(system, key="window-attempt-newer")
    assert secrets.creates == provider.calls == 7


def test_concurrent_fifth_reservation_cas_cannot_create_sixth_secret(system, monkeypatch):
    service, account, dynamo, secrets, provider, *_ = system
    for index in range(4):
        validate(system, key=f"race-first-attempt-{index:03d}")
    original = dynamo.get_item
    raced = False

    def interleave(**request):
        nonlocal raced
        result = original(**request)
        if request["Key"]["PK"]["S"] == budget_key(account.owner_id) and not raced:
            raced = True
            # This contender commits after the caller read its old budget. Both services
            # traverse the real store's conditional transaction against the same fake table.
            validate(system, key="race-winning-attempt")
        return result

    monkeypatch.setattr(dynamo, "get_item", interleave)
    with pytest.raises(ConnectionConflict):
        validate(system, key="race-losing-attempt1")
    assert len(budget(system).attempted_at) == secrets.creates == provider.calls == 5
    with pytest.raises(ConnectionRateLimited):
        validate(system, key="race-losing-attempt1")
    assert secrets.creates == 5


def test_selection_race_fences_validation_reservation_before_secret_creation(system, monkeypatch):
    service, account, dynamo, secrets, provider, *_ = system
    first = validate(system)
    original = dynamo.transact_write_items
    raced = False

    def interleave(**request):
        nonlocal raced
        if len(request["TransactItems"]) == 3 and not raced:
            raced = True
            select(system, first)
        return original(**request)

    monkeypatch.setattr(dynamo, "transact_write_items", interleave)
    with pytest.raises(ConnectionConflict):
        validate(system, key="raced-after-selection")
    assert secrets.creates == 1 and provider.calls == 2
    assert len(budget(system).attempted_at) == 1
    assert service.get(account)["state"] == "ready"


def test_rate_limit_http_response_is_stable_and_credential_free(system):
    service, account, dynamo, secrets, provider, *_ = system
    provider.list_shops = Mock(side_effect=InvalidCredential)
    for index in range(5):
        with pytest.raises(InvalidCredential):
            validate(system, key=f"http-cap-attempt-{index:03d}")
    accounts = Mock()
    accounts.return_value.get.return_value = account
    handler = ConnectionHttpHandler(
        config=service.config,
        account_reader_factory=accounts,
        service_factory=lambda: service,
        allowed_operation="validate",
    )
    result = handler(
        event(
            service.config,
            "validate",
            {
                "token": TOKEN,
                "authorization": AUTHORIZATION,
            },
        )
    )
    assert result["statusCode"] == 429
    assert json.loads(result["body"])["error"]["code"] == "CONNECTION_RATE_LIMITED"
    assert "fifteen minutes" in result["body"]
    assert TOKEN not in json.dumps(result) + json.dumps(dynamo.items)
    assert secrets.creates == provider.list_shops.call_count == 5


def test_cleanup_rechecks_binding_on_retry_and_does_not_delete_promoted_secret(system):
    from mr_lister.connections.cleanup import CandidateCleanupService, CandidateSecretDisposer
    from mr_lister.connections.models import Connection, canonical
    from mr_lister.connections.store import fence

    service, account, dynamo, _, _, _, clock = system
    candidate = validate(system)
    record = service.store.get_candidate(account.owner_id, candidate["candidate_id"])
    clock[0] += 1801
    deleting = service.store.claim_cleanup(record)
    binding = StoreBindingAuthority.create(
        owner_id=account.owner_id,
        connection_id=record.connection_id,
        shop_binding_id=record.shop_binding_id,
        shop_id=7,
        authorization_epoch=1,
    )
    active = Connection(
        binding=binding,
        candidate_id=record.candidate_id,
        secret=record.secret,
        store=record.stores[0],
        state="active",
    )
    key = connection_key(binding)
    dynamo.items[key] = {
        "PK": {"S": key},
        "entity_type": {"S": "Connection"},
        "payload": {"S": canonical(active)},
        **fence(active),
    }
    client = Mock()
    cleanup = CandidateCleanupService(
        store=service.store,
        disposer=CandidateSecretDisposer(client, service.config),
        clock=lambda: clock[0],
    )
    with pytest.raises(ConnectionConflict):
        cleanup.cleanup(owner_id=account.owner_id, candidate_id=deleting.candidate_id)
    client.delete_secret.assert_not_called()


def test_absent_secret_cleanup_can_complete_after_validation_failed_before_secret_creation(system):
    from mr_lister.connections.cleanup import CandidateCleanupService, CandidateSecretDisposer

    service, account, _, _, _, _, clock = system
    service.credentials.save = Mock(side_effect=ConnectionError)
    with pytest.raises(ConnectionError):
        validate(system)
    candidate_id = (
        "candidate_" + operation_id(account.owner_id, "validation-key-0001", "validate")[:32]
    )
    clock[0] += 1801
    client = Mock()
    client.delete_secret.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": TOKEN}}, "DeleteSecret"
    )
    cleanup = CandidateCleanupService(
        store=service.store,
        disposer=CandidateSecretDisposer(client, service.config),
        clock=lambda: clock[0],
    )
    assert cleanup.cleanup(owner_id=account.owner_id, candidate_id=candidate_id) == "deleted"


def test_lost_activation_marker_response_is_recovered_without_regrant(system):
    service, _, dynamo, _, _, groups, _ = system
    original = groups.grant

    def grant(account):
        original(account)
        dynamo.lose_next = True

    groups.grant = grant
    candidate = validate(system)
    assert select(system, candidate)["state"] == "ready"
    assert groups.calls == [SUBJECT]
    assert select(system, candidate)["state"] == "ready"
    assert groups.calls == [SUBJECT]
