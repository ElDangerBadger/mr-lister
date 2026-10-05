"""Opt-in composition checks for the actual API, provider, and enabled publication factories."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from mr_lister.cloud import phase6_composition as api
from mr_lister.cloud import phase6_machine_composition as machine
from mr_lister.cloud import phase718_composition as publication
from mr_lister.cloud import phase718_entrypoints
from mr_lister.cloud.connection_composition import (
    RoutedConnectionResolver,
    load_connection_configuration,
    load_legacy_owner_ids,
)
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.connections.models import ConnectionError
from mr_lister.connections.store import DynamoConnectionDirectory
from tests.test_phase6_cloud_api import create_upload_body
from tests.test_phase66_api_composition import RecordingClientFactory
from tests.test_phase66_api_composition import api_event as composition_event
from tests.test_phase66_api_composition import exact_environment as api_env
from tests.test_phase66_machine_composition import RecordingFactory
from tests.test_phase66_machine_composition import _base_environment as machine_env
from tests.test_phase718_enabled_backend import (
    _Credentials,
    _Dynamo,
    _Factory,
    _Transport,
)
from tests.test_phase718_enabled_backend import (
    exact_environment as publication_env,
)

LEGACY = "a" * 64


def connected(environment):
    result = dict(environment)
    issuer = result.setdefault(
        "MR_LISTER_COGNITO_ISSUER",
        "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_PrimaryPool",
    )
    client = result.setdefault("MR_LISTER_COGNITO_CLIENT_ID", "primaryclient123")
    result.update(
        {
            "MR_LISTER_CONNECTION_ENABLED": "true",
            "MR_LISTER_CONNECTION_WORKFLOW_ENABLED": "true",
            "MR_LISTER_CONNECTION_TABLE_NAME": "mr-lister-connections-dev",
            "MR_LISTER_CONNECTION_SECRET_PREFIX": "mr-lister/dev/connections/",
            "MR_LISTER_ACCOUNT_USER_POOL_ID": issuer.rsplit("/", 1)[1],
            "MR_LISTER_ACCOUNT_CLIENT_ID": client,
            "MR_LISTER_ACCOUNT_TABLE_NAME": "mr-lister-account-dev",
            "MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS": json.dumps([LEGACY], separators=(",", ":")),
        }
    )
    return result


def test_flag_absent_or_false_keeps_api_legacy_configuration_without_new_dependencies():
    for value in (None, "false"):
        env = api_env()
        if value is not None:
            env = connected(env)
            env["MR_LISTER_CONNECTION_ENABLED"] = value
        config = api.load_command_api_configuration(env)
        assert config.common.connections is None
        factory = RecordingClientFactory()
        adapter = api.compose_command_api_adapter(config, client_factory=factory)
        assert adapter._commands.store._binding_guard is None
        assert factory.calls == [("dynamodb", "us-west-2")]
        assert factory.dynamodb.operations == []


def test_disabled_staged_upload_keeps_legacy_whitelist_and_denies_native_before_data_or_s3():
    env = connected(api_env())
    env["MR_LISTER_CONNECTION_ENABLED"] = "false"
    factory = RecordingClientFactory()
    handler = api.compose_upload_api_adapter(
        api.load_upload_api_configuration(env),
        client_factory=factory,
    )
    assert handler._legacy_owner_ids == frozenset({LEGACY})
    assert handler._binding_authority is None
    for groups in ('["seller"]', '["seller","account"]'):
        event = composition_event(
            "POST /v1/uploads",
            subject="new-native-account",
            body=create_upload_body(),
        )
        event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = groups
        event["headers"] = {
            "Content-Type": "application/json",
            "Idempotency-Key": "rollback-boundary-1",
        }
        assert handler(event)["statusCode"] == 422
        assert not factory.dynamodb.operations and not factory.s3.operations


def test_disabled_staged_configuration_without_exact_reserved_authority_fails_before_clients():
    for change in ("missing", "wrong_pool", "wrong_client"):
        env = connected(api_env())
        env["MR_LISTER_CONNECTION_ENABLED"] = "false"
        if change == "missing":
            del env["MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS"]
        elif change == "wrong_pool":
            env["MR_LISTER_ACCOUNT_USER_POOL_ID"] = "us-west-2_OtherPool"
        else:
            env["MR_LISTER_ACCOUNT_CLIENT_ID"] = "otherclient"
        factory = RecordingClientFactory()
        with pytest.raises(api.Phase6ApiConfigurationError):
            api.build_upload_api_handler(env, client_factory=factory)
        assert not factory.calls


def test_truly_old_upload_environment_still_denies_account_group_before_any_data_or_s3():
    factory = RecordingClientFactory()
    handler = api.compose_upload_api_adapter(
        api.load_upload_api_configuration(api_env()),
        client_factory=factory,
    )
    assert handler._legacy_owner_ids is None and handler._binding_authority is None
    event = composition_event(
        "POST /v1/uploads",
        subject="new-native-account",
        body=create_upload_body(),
    )
    event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = ["seller", "account"]
    event["headers"] = {
        "Content-Type": "application/json",
        "Idempotency-Key": "rollback-boundary-1",
    }
    assert handler(event)["statusCode"] == 422
    assert not factory.dynamodb.operations and not factory.s3.operations


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_ACCOUNT_USER_POOL_ID", "us-west-2_OtherPool"),
        ("MR_LISTER_ACCOUNT_CLIENT_ID", "otherclient"),
        ("MR_LISTER_ACCOUNT_TABLE_NAME", "mr-lister-account-prod"),
        ("MR_LISTER_CONNECTION_TABLE_NAME", "mr-lister-connections-prod"),
        ("MR_LISTER_CONNECTION_SECRET_PREFIX", "mr-lister/prod/connections/"),
        ("MR_LISTER_CONNECTION_ENABLED", "TRUE"),
        ("MR_LISTER_CONNECTION_WORKFLOW_ENABLED", "TRUE"),
        ("MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS", "[]"),
        ("MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS", json.dumps([f"{i:064x}" for i in range(1, 42)])),
    ],
)
def test_optin_rejects_configuration_drift_before_clients(name, value):
    environment = connected(api_env())
    environment[name] = value
    factory = RecordingClientFactory()
    with pytest.raises(api.Phase6ApiConfigurationError):
        api.build_command_api_handler(environment, client_factory=factory)
    assert factory.calls == []


def test_enabled_api_query_and_command_only_construct_dynamo_and_s3_and_attach_guard():
    env = connected(api_env())
    query_factory, command_factory = RecordingClientFactory(), RecordingClientFactory()
    query = api.compose_query_api_adapter(
        api.load_query_api_configuration(env), client_factory=query_factory
    )
    command = api.compose_command_api_adapter(
        api.load_command_api_configuration(env), client_factory=command_factory
    )
    for store in (query._store._store, command._commands.store):
        assert isinstance(store._binding_guard, DynamoConnectionDirectory)
        assert store._binding_guard.config.table_name == "mr-lister-connections-dev"
    assert query_factory.calls == [("dynamodb", "us-west-2"), ("s3", "us-west-2")]
    assert command_factory.calls == [("dynamodb", "us-west-2")]
    assert query_factory.dynamodb.operations == command_factory.dynamodb.operations == []


def test_provider_factory_composes_exact_and_static_legacy_resolvers_with_same_directory():
    factory = RecordingFactory()
    handler = machine.build_provider_handler(connected(machine_env()), client_factory=factory)
    worker = handler._provider
    directory = worker._store._binding_guard
    assert isinstance(directory, DynamoConnectionDirectory)
    assert worker._resources._legacy_owner_ids == frozenset({LEGACY})
    router = worker._resources._connection_resolver
    assert isinstance(router, RoutedConnectionResolver)
    assert router._modern._directory is directory
    assert router._modern._credentials._client is factory.clients["secretsmanager"]
    assert factory.calls == [
        ("dynamodb", "us-west-2"),
        ("s3", "us-west-2"),
        ("secretsmanager", "us-west-2"),
    ]


def test_only_provider_role_requires_legacy_allowlist_before_sdk():
    env = machine_env()
    env.pop("MR_LISTER_LEGACY_OWNER_IDS")
    factory = RecordingFactory()
    with pytest.raises(machine.Phase6MachineConfigurationError):
        machine.build_provider_handler(env, client_factory=factory)
    assert factory.calls == []
    assert machine.load_settlement_configuration(env).common.connections is None
    assert machine.load_preparation_configuration(env).common.connections is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "[]",
        '["bad"]',
        '["' + LEGACY + '","' + LEGACY + '"]',
        json.dumps(["b" * 64, LEGACY]),
        '["' + "0" * 64 + '"]',
    ],
)
def test_legacy_allowlist_missing_malformed_duplicate_or_noncanonical_fails_closed(raw):
    with pytest.raises(ConnectionError):
        load_legacy_owner_ids({"MR_LISTER_LEGACY_OWNER_IDS": raw}, None)


def test_enabled_legacy_allowlist_derives_reserved_or_requires_exact_equivalence():
    env = connected(api_env())
    config = load_connection_configuration(env, region="us-west-2", environment_name="dev")
    assert load_legacy_owner_ids(env, config) == frozenset({LEGACY})
    env["MR_LISTER_LEGACY_OWNER_IDS"] = '["' + LEGACY + '"]'
    assert load_legacy_owner_ids(env, config) == frozenset({LEGACY})
    env["MR_LISTER_LEGACY_OWNER_IDS"] = '["' + "b" * 64 + '"]'
    with pytest.raises(ConnectionError):
        load_legacy_owner_ids(env, config)


def test_router_never_uses_current_selection_or_legacy_resolver_for_modern_binding():
    env = connected(api_env())
    config = load_connection_configuration(env, region="us-west-2", environment_name="dev")
    legacy, directory, secrets = Mock(), Mock(), Mock()
    router = RoutedConnectionResolver(
        legacy=legacy, directory=directory, secrets=secrets, config=config
    )
    modern = Mock()
    router._modern = modern
    binding = StoreBindingAuthority.create(
        owner_id="b" * 64,
        connection_id="conn_" + "c" * 32,
        shop_binding_id="binding_" + "d" * 32,
        shop_id=7,
        authorization_epoch=1,
    )
    assert router.resolve_exact(binding=binding) is modern.resolve_exact.return_value
    modern.resolve_exact.assert_called_once_with(binding=binding)
    legacy.resolve.assert_not_called()
    with pytest.raises(ConnectionError):
        router.resolve(owner_id=binding.owner_id)
    legacy.resolve.assert_not_called()
    assert router.resolve(owner_id=LEGACY) is legacy.resolve.return_value
    legacy.resolve.assert_called_once_with(owner_id=LEGACY)
    reserved_binding = StoreBindingAuthority.create(
        owner_id=LEGACY,
        connection_id="conn_" + "c" * 32,
        shop_binding_id="binding_" + "d" * 32,
        shop_id=7,
        authorization_epoch=1,
    )
    with pytest.raises(ConnectionError):
        router.resolve_exact(binding=reserved_binding)
    assert modern.resolve_exact.call_count == 1


def test_current_enabled_publication_request_and_worker_both_receive_epoch_guard_without_io():
    env = connected(publication_env())
    factory = _Factory()
    request = publication.build_phase718_request_handler(env, client_factory=factory)
    request_store = request._delegate._requests.store
    assert isinstance(request_store._binding_guard, DynamoConnectionDirectory)
    dynamo, credentials, transport = _Dynamo(), _Credentials(), _Transport()
    worker = publication.build_phase718_worker_handler(
        env,
        dynamodb=dynamo,
        credentials=credentials,
        transport=transport,
        rejected_audit_writer=lambda _: None,
    )
    store = worker._coordinator._store
    assert isinstance(store._binding_guard, DynamoConnectionDirectory)
    assert store._request_store._binding_guard is store._binding_guard
    assert factory.client.calls == dynamo.calls == []
    assert credentials.calls == transport.calls == 0


def test_real_enabled_worker_routes_exact_and_explicit_legacy_resolvers(
    monkeypatch,
):
    import boto3

    env = connected(publication_env())
    env["MR_LISTER_PRINTIFY_SECRET_ARN"] = (
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:mr-lister/phase6/dev/printify-AbCd12"
    )
    config = publication.load_phase718_enabled_configuration(env)
    monkeypatch.setattr(phase718_entrypoints, "_verified_configuration", lambda _: (env, config))
    dynamo, secrets = _Dynamo(), Mock()
    client = Mock(
        side_effect=lambda name, **_: {"dynamodb": dynamo, "secretsmanager": secrets}[name]
    )
    monkeypatch.setattr(boto3, "client", client)
    capture = Mock(return_value=object())
    monkeypatch.setattr(publication, "compose_phase718_worker_handler", capture)
    phase718_entrypoints._build_worker()
    values = capture.call_args.kwargs
    authority = values["credentials"]
    assert authority._legacy_owner_ids == frozenset({LEGACY})
    assert isinstance(authority._connections, RoutedConnectionResolver)
    assert authority._connections._modern._directory.config == values["connections"]
    assert client.call_count == 2
    secrets.get_secret_value.assert_not_called()
    assert dynamo.calls == []


def test_enabled_worker_missing_legacy_configuration_fails_before_client_creation(monkeypatch):
    import boto3

    env = publication_env()
    env["MR_LISTER_PRINTIFY_SECRET_ARN"] = (
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:mr-lister/phase6/dev/printify-AbCd12"
    )
    config = publication.load_phase718_enabled_configuration(env)
    monkeypatch.setattr(phase718_entrypoints, "_verified_configuration", lambda _: (env, config))
    client = Mock(side_effect=AssertionError)
    monkeypatch.setattr(boto3, "client", client)
    with pytest.raises(ConnectionError):
        phase718_entrypoints._build_worker()
    client.assert_not_called()


def test_enabled_upload_shares_directory_between_http_selection_and_atomic_store_guard():
    factory = RecordingClientFactory()
    adapter = api.compose_upload_api_adapter(
        api.load_upload_api_configuration(connected(api_env())), client_factory=factory
    )
    assert isinstance(adapter._binding_authority, DynamoConnectionDirectory)
    assert adapter._uploads._store._binding_guard is adapter._binding_authority
    assert adapter._legacy_owner_ids == frozenset({LEGACY})
    assert not factory.dynamodb.operations
    assert factory.calls == [("dynamodb", "us-west-2"), ("s3", "us-west-2")]


def test_preparation_machine_reads_typed_jobs_without_secret_client():
    factory = RecordingFactory()
    handler = machine.build_preparation_handler(connected(machine_env()), client_factory=factory)
    assert isinstance(handler._preparation._store._binding_guard, DynamoConnectionDirectory)
    assert factory.calls == [("dynamodb", "us-west-2"), ("bedrock-agentcore", "us-west-2")]


def test_agentcore_optin_adds_only_read_directory_and_preserves_models(monkeypatch):
    from mr_lister.agent import phase6_composition as runtime
    from tests.test_phase66_agentcore_composition import (
        FakeDynamo,
        FakeIntelligence,
        FakeS3,
        _environment,
    )

    old = runtime.load_phase6_agentcore_configuration(_environment())
    configured = runtime.load_phase6_agentcore_configuration(connected(_environment()))
    assert configured.intelligence == old.intelligence
    assert configured.controller_model_id == old.controller_model_id
    assert configured.intelligence_fingerprint == old.intelligence_fingerprint
    capture = Mock(return_value=object())
    monkeypatch.setattr(runtime, "create_phase6_agentcore_runtime", capture)
    runtime.compose_phase6_agentcore_runtime(
        configured,
        dynamodb_client=FakeDynamo(),
        s3_client=FakeS3(),
        intelligence=FakeIntelligence(),
        controller_model=configured.controller_model_id,
    )
    backend = capture.call_args.kwargs["backend"]
    assert isinstance(backend._store._binding_guard, DynamoConnectionDirectory)
    assert not hasattr(backend._store._binding_guard, "_credentials")
