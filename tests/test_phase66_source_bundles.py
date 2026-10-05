from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.build_phase66_source_bundles import (
    build_source_bundles,
    render_deterministic_zip,
    verify_source_bundle,
)


def _destination(tmp_path: Path, name: str) -> Path:
    return tmp_path / name / "phase6-release"


def test_source_bundles_are_reproducible_and_manifest_bound(tmp_path: Path) -> None:
    first_lambda, first_agentcore = build_source_bundles(_destination(tmp_path, "first"))
    second_lambda, second_agentcore = build_source_bundles(_destination(tmp_path, "second"))

    assert (first_lambda / "source-manifest.json").read_bytes() == (
        second_lambda / "source-manifest.json"
    ).read_bytes()
    assert (first_agentcore / "source-manifest.json").read_bytes() == (
        second_agentcore / "source-manifest.json"
    ).read_bytes()
    verify_source_bundle(first_lambda)
    verify_source_bundle(first_agentcore)


def test_manifest_has_only_relative_sha256_size_records(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "manifest"))

    for root in (lambda_root, agentcore_root):
        payload = json.loads((root / "source-manifest.json").read_text(encoding="utf-8"))
        assert payload["algorithm"] == "sha256"
        assert payload["format"] == "phase6-source-v1"
        assert payload["files"]
        assert "source-manifest.json" not in {item["path"] for item in payload["files"]}
        for item in payload["files"]:
            assert set(item) == {"path", "sha256", "size_bytes"}
            assert not Path(item["path"]).is_absolute()
            assert ".." not in Path(item["path"]).parts
            assert len(item["sha256"]) == 64
            assert item["size_bytes"] == (root / item["path"]).stat().st_size
        assert (root / "dependency-build-request.json").is_file()
        assert (root / "mr_lister/release/phase6.py").is_file()
        assert (root / "mr_lister/__init__.py").read_bytes() == b""
        assert (root / "mr_lister/release/__init__.py").read_bytes() == b""
        assert {path.name for path in (root / "mr_lister/release").glob("*.py")} == {
            "__init__.py",
            "phase6.py",
        }
        assert not (root / "release-manifest.json").exists()


def test_tamper_or_extra_file_fails_verification(tmp_path: Path) -> None:
    lambda_root, _agentcore = build_source_bundles(_destination(tmp_path, "tamper"))
    target = lambda_root / "phase6_lambda.py"
    target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest does not match"):
        verify_source_bundle(lambda_root)

    other_lambda, _ = build_source_bundles(_destination(tmp_path, "extra"))
    (other_lambda / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest does not match"):
        verify_source_bundle(other_lambda)


def test_existing_or_wrongly_named_destination_is_never_overwritten(tmp_path: Path) -> None:
    existing = _destination(tmp_path, "existing")
    existing.mkdir(parents=True)
    marker = existing / "user-data.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="new phase6-release"):
        build_source_bundles(existing)
    with pytest.raises(ValueError, match="new phase6-release"):
        build_source_bundles(tmp_path / "wrong-name")

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_lambda_bundle_excludes_agentcore_and_legacy_broad_surfaces(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "lambda-surface"))

    assert (lambda_root / "phase6_lambda.py").is_file()
    assert (lambda_root / "mr_lister/cloud/evaluator_publication.py").is_file()
    assert (lambda_root / "mr_lister/cloud/printify_secret_contract.py").is_file()
    assert (lambda_root / "mr_lister/cloud/workspace_history.py").is_file()
    assert not (agentcore_root / "mr_lister/cloud/printify_secret_contract.py").exists()
    assert not (agentcore_root / "mr_lister/cloud/workspace_history.py").exists()
    assert not (lambda_root / "mr_lister/publication").exists()
    assert (lambda_root / "mr_lister/cloud/phase6_entrypoints.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_retention_entrypoint.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_operational_cleanup_entrypoint.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_execution_recovery_composition.py").is_file()
    assert (lambda_root / "mr_lister/cloud/phase6_execution_recovery_entrypoint.py").is_file()
    assert (lambda_root / "mr_lister/agent/runtime_binding.py").is_file()
    assert (lambda_root / "mr_lister/production/operational_cleanup.py").is_file()
    assert (lambda_root / "mr_lister/production/operational_cleanup_aws.py").is_file()
    assert (lambda_root / "mr_lister/production/retention.py").is_file()
    assert (lambda_root / "mr_lister/production/retention_aws.py").is_file()
    assert not (lambda_root / "mr_lister/api").exists()
    assert not (lambda_root / "mr_lister/production/adapter.py").exists()
    assert not (lambda_root / "mr_lister/workflow/service.py").exists()
    requirements = (lambda_root / "requirements.txt").read_text(encoding="utf-8")
    assert "strands" not in requirements
    assert "agentcore" not in requirements
    assert "fastapi" not in requirements


def test_agentcore_bundle_is_phase6_gemma_strands_not_phase3_synthetic(tmp_path: Path) -> None:
    _lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "agentcore-surface"))

    main = (agentcore_root / "main.py").read_text(encoding="utf-8")
    assert "build_phase6_agentcore_runtime" in main
    assert "verify_phase6_packaged_release" in main
    assert "build_synthetic_canary_runtime" not in main
    assert (agentcore_root / "config/bedrock/google_gemma_3_27b_it.json").is_file()
    active_config = agentcore_root / "config/bedrock/google_gemma_4_31b.json"
    assert json.loads(active_config.read_text(encoding="utf-8")) == {
        "transport": "mantle",
        "region": "us-west-2",
        "model_id": "google.gemma-4-31b",
        "output_mode": "native_json_schema",
        "max_tokens": 2048,
        "temperature": 0.0,
        "max_repair_attempts": 2,
    }
    assert not (agentcore_root / "config/bedrock/google_gemma_4_31b_candidate.json").exists()
    assert not (agentcore_root / "tools").exists()
    assert not (agentcore_root / "mr_lister/cloud").exists()
    assert not (agentcore_root / "mr_lister/production").exists()
    assert not (agentcore_root / "mr_lister/workflow/service.py").exists()
    requirements = (agentcore_root / "requirements.txt").read_text(encoding="utf-8")
    assert "strands-agents" in requirements
    assert "bedrock-agentcore" in requirements


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink contract requires POSIX support")
def test_verifier_rejects_a_symlink_even_when_manifest_is_untouched(tmp_path: Path) -> None:
    lambda_root, _agentcore = build_source_bundles(_destination(tmp_path, "symlink"))
    os.symlink(lambda_root / "requirements.txt", lambda_root / "linked.txt")

    with pytest.raises(ValueError):
        verify_source_bundle(lambda_root)


def test_bundled_module_imports_do_not_eager_load_legacy_publish_surfaces(tmp_path: Path) -> None:
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "imports"))
    interpreter = Path(sys.executable)

    lambda_result = subprocess.run(
        [
            interpreter,
            "-c",
            (
                "import sys; import mr_lister.cloud.phase6_entrypoints; "
                "import mr_lister.cloud.phase6_execution_recovery_entrypoint; "
                "import mr_lister.cloud.phase6_operational_cleanup_entrypoint; "
                "import mr_lister.cloud.phase6_retention_entrypoint; "
                "assert 'mr_lister.production.adapter' not in sys.modules; "
                "assert 'mr_lister.workflow.service' not in sys.modules"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(lambda_root)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert lambda_result.returncode == 0, lambda_result.stderr

    agentcore_result = subprocess.run(
        [
            interpreter,
            "-c",
            (
                "import sys; import mr_lister.agent.phase6_composition; "
                "assert 'mr_lister.production.adapter' not in sys.modules; "
                "assert 'mr_lister.workflow.service' not in sys.modules"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(agentcore_root)},
        capture_output=True,
        check=False,
        text=True,
    )
    assert agentcore_result.returncode == 0, agentcore_result.stderr


@pytest.mark.parametrize(
    "omit_module", [None, "harness_production.py", "harness_candidate.py", "mantle.py"]
)
def test_packaged_gemma4_runtime_imports_and_selects_frozen_harness_without_checkout_fallback(
    tmp_path: Path, omit_module: str | None
) -> None:
    _lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "packaged-gemma4"))
    if omit_module is not None:
        (agentcore_root / "mr_lister/intelligence" / omit_module).unlink()
    code = """
import sys
from pathlib import Path
from types import SimpleNamespace
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import boto3
def no_session(*args, **kwargs):
    raise AssertionError('Packaged adapter construction attempted AWS access')
boto3.Session = no_session
from mr_lister.intelligence import harness_production as production
from mr_lister.intelligence.harness_candidate import (
    candidate_prompt_bundles, VerifiedProductContext,
)
from mr_lister.intelligence.mantle import MAX_REQUEST_BYTES
from mr_lister.intelligence.settings import BedrockSettings
settings = BedrockSettings.model_validate_json(
    (root / 'config/bedrock/google_gemma_4_31b.json').read_bytes()
)
assert settings.transport == 'mantle'
assert settings.model_id == 'google.gemma-4-31b'
assert production.PRODUCTION_HARNESS_REVISION == 'v6'
assert production.PRODUCTION_HARNESS_PROMPT_FINGERPRINT == (
    candidate_prompt_bundles(revision='v6')['full'].fingerprint
)
assert MAX_REQUEST_BYTES == 3500000
calls = []
def candidate_factory(*args, **kwargs):
    calls.append((args, kwargs))
    return SimpleNamespace()
production.build_harness_candidate_adapter = candidate_factory
adapter = production.build_harness_production_adapter(settings, session=object())
assert callable(adapter.prepare_listing)
assert len(calls) == 1
assert calls[0][1]['revision'] == 'v6'
assert isinstance(calls[0][1]['product_context'], VerifiedProductContext)
assert all(Path(module.__file__).resolve().is_relative_to(root)
    for name, module in sys.modules.items()
    if name.startswith('mr_lister') and getattr(module, '__file__', None))
assert 'mr_lister.production' not in sys.modules
assert 'mr_lister.publication' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(agentcore_root)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    if omit_module is not None:
        assert result.returncode != 0, "Packaged runtime silently used checkout source"
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("omit_history", [False, True])
def test_packaged_api_composition_imports_its_history_dependency_without_checkout_fallback(
    tmp_path: Path, omit_history: bool
) -> None:
    lambda_root, _ = build_source_bundles(_destination(tmp_path, "packaged-api"))
    if omit_history:
        # Reproduce the packaging regression: lazy entrypoint imports alone miss it.
        (lambda_root / "mr_lister/cloud/workspace_history.py").unlink()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; from pathlib import Path; "
                "root = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(root)); "
                "import mr_lister.cloud.api; import mr_lister.cloud.phase6_composition; "
                "import mr_lister.cloud.workspace_history; "
                "assert all(Path(module.__file__).resolve().is_relative_to(root) "
                "for name, module in sys.modules.items() "
                "if name.startswith('mr_lister') and getattr(module, '__file__', None)); "
                "assert 'mr_lister.publication' not in sys.modules; "
                "assert 'mr_lister.judge_cleanup' not in sys.modules; "
                "assert 'mr_lister.judge_session' not in sys.modules"
            ),
            str(lambda_root),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    if omit_history:
        assert result.returncode != 0, "Packaged import silently used checkout source"
    else:
        assert result.returncode == 0, result.stderr


def test_account_connection_inventory_is_explicit_and_runtime_remains_metadata_only(tmp_path):
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "new-inventory"))
    account_names = {
        "__init__.py",
        "models.py",
        "store.py",
        "provision.py",
        "provision_entrypoint.py",
        "query.py",
        "query_entrypoint.py",
    }
    connection_names = {
        "__init__.py",
        "binding.py",
        "configuration.py",
        "models.py",
        "store.py",
        "credentials.py",
        "transport.py",
        "service.py",
        "http.py",
        "cleanup.py",
        "entrypoint.py",
    }
    for root, accounts, connections in (
        (lambda_root, account_names, connection_names),
        (
            agentcore_root,
            {"__init__.py", "models.py"},
            {"__init__.py", "binding.py", "configuration.py", "models.py", "store.py"},
        ),
    ):
        assert {path.name for path in (root / "mr_lister/accounts").iterdir()} == accounts
        assert {path.name for path in (root / "mr_lister/connections").iterdir()} == connections
        manifest = json.loads((root / "source-manifest.json").read_bytes())
        inventory = {item["path"] for item in manifest["files"]}
        assert {"mr_lister/accounts/" + name for name in accounts} <= inventory
        assert {"mr_lister/connections/" + name for name in connections} <= inventory
    assert (lambda_root / "mr_lister/cloud/connection_composition.py").is_file()
    assert not (agentcore_root / "mr_lister/cloud").exists()
    assert not (agentcore_root / "mr_lister/production").exists()


@pytest.mark.parametrize(
    "component,omitted",
    [
        ("lambda", None),
        ("agentcore", None),
        ("lambda", "mr_lister/connections/binding.py"),
        ("lambda", "mr_lister/accounts/models.py"),
        ("agentcore", "mr_lister/connections/configuration.py"),
        ("agentcore", "mr_lister/connections/binding.py"),
    ],
)
def test_source_zips_import_new_handlers_and_bound_dtos_without_checkout_or_sdk_access(
    tmp_path,
    component,
    omitted,
):
    lambda_root, agentcore_root = build_source_bundles(_destination(tmp_path, "zip-import"))
    source = lambda_root if component == "lambda" else agentcore_root
    if omitted:
        (source / omitted).unlink()
    # This source ZIP check is not a deployment seal or ARM64 wheel attestation.
    archive = tmp_path / (component + "-source.zip")
    archive.write_bytes(render_deterministic_zip(source))
    code = r"""
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
archive, source, component = sys.argv[1:]
sys.path.insert(0, archive)
for name in list(os.environ):
    if name.startswith('MR_LISTER_'):
        del os.environ[name]
import boto3
sdk_attempts = []
def deny_sdk(*args, **kwargs):
    sdk_attempts.append(True)
    raise AssertionError('Source ZIP import attempted SDK or provider access')
class NoSession:
    def __init__(self, *args, **kwargs):
        deny_sdk()
boto3.Session = boto3.session.Session = NoSession
boto3.client = boto3.resource = deny_sdk
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.connections.configuration import load_connection_configuration, connection_directory
from mr_lister.connections.store import DynamoConnectionDirectory
from mr_lister.control.models import SourceArtifactRecord
binding = StoreBindingAuthority.create(owner_id='a' * 64, connection_id='conn_' + 'b' * 32,
    shop_binding_id='binding_' + 'c' * 32, shop_id=321, authorization_epoch=2)
payload = dict(job_id='job-one', owner_id='a' * 64, fingerprint='d' * 64, bucket='source-bucket',
    object_key='private/owners/' + 'a' * 64 + '/jobs/job-one/source/source.png', version_id='v1',
    content_sha256='e' * 64, size_bytes=123, product_profile_id='gildan_64000_swiftpod',
    product_profile_version=2, product_profile_fingerprint='f' * 64, created_at=datetime.now(UTC))
legacy = SourceArtifactRecord(**payload)
assert 'store_binding' not in legacy.model_dump(mode='json')
modern = SourceArtifactRecord(**payload, store_binding=binding)
assert SourceArtifactRecord.model_validate_json(modern.model_dump_json()).store_binding == binding
bad = modern.model_dump(mode='python')
bad['store_binding'] = binding.model_dump(mode='python')
bad['store_binding']['shop_id'] = 999
try:
    SourceArtifactRecord.model_validate(bad)
except ValueError:
    pass
else:
    raise AssertionError('ZIP model accepted destination fingerprint drift')
environment = {
    'MR_LISTER_CONNECTION_ENABLED': 'true',
    'MR_LISTER_CONNECTION_WORKFLOW_ENABLED': 'true',
    'MR_LISTER_ACCOUNT_USER_POOL_ID': 'us-west-2_TestPool',
    'MR_LISTER_ACCOUNT_CLIENT_ID': 'testclient123',
    'MR_LISTER_ACCOUNT_TABLE_NAME': 'mr-lister-account-dev',
    'MR_LISTER_ACCOUNT_RESERVED_OWNER_IDS': '["' + 'f' * 64 + '"]',
    'MR_LISTER_CONNECTION_TABLE_NAME': 'mr-lister-connections-dev',
    'MR_LISTER_CONNECTION_SECRET_PREFIX': 'mr-lister/dev/connections/',
    'MR_LISTER_COGNITO_ISSUER': 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_TestPool',
    'MR_LISTER_COGNITO_CLIENT_ID': 'testclient123',
}
config = load_connection_configuration(environment, region='us-west-2', environment_name='dev')
class FakeDynamo:
    get_item = put_item = transact_write_items = deny_sdk
class FakeS3:
    get_object = deny_sdk
assert isinstance(connection_directory(config, FakeDynamo()), DynamoConnectionDirectory)
if component == 'lambda':
    import phase6_lambda
    from mr_lister.cloud import phase6_composition, phase6_machine_composition
    from mr_lister.accounts import query_entrypoint, provision_entrypoint
    from mr_lister.connections import entrypoint, cleanup
    assert query_entrypoint.lambda_handler({})['statusCode'] == 503
    event = {'triggerSource': 'PreAuthentication_Authentication'}
    assert provision_entrypoint.lambda_handler(event) is event
    for handler in (entrypoint.query_handler, entrypoint.validate_handler,
                    entrypoint.select_handler, entrypoint.activate_handler):
        assert handler({})['statusCode'] == 503
    assert entrypoint.cleanup_handler({}) == {'processed': 0, 'completed': 0, 'failed': 0}
else:
    from mr_lister.agent import phase6_composition as runtime
    from mr_lister.review_profile import FilesystemReviewProductAuthority
    exact = FilesystemReviewProductAuthority(
        profile_directory=Path(source) / 'config/product_profiles').get_exact(
        profile_id='gildan_64000_swiftpod', profile_version=2)
    configured = SimpleNamespace(
        state_table='mr-lister-phase6-dev', artifact_bucket='source-bucket',
        account_id='123456789012', profile=SimpleNamespace(exact=exact), connections=config,
        controller_model_id=runtime.PHASE6_STRANDS_CONTROLLER_MODEL_ID, judge_pricing_policy=None)
    runtime.create_phase6_agentcore_runtime = lambda **kwargs: kwargs
    result = runtime.compose_phase6_agentcore_runtime(
        configured, dynamodb_client=FakeDynamo(), s3_client=FakeS3(),
        intelligence=SimpleNamespace(prepare_listing=deny_sdk),
        controller_model=configured.controller_model_id)
    assert isinstance(result['backend']._store._binding_guard, DynamoConnectionDirectory)
    assert not hasattr(result['backend']._store._binding_guard, '_credentials')
    assert not any(name.startswith(('mr_lister.cloud', 'mr_lister.production',
        'mr_lister.connections.credentials', 'mr_lister.connections.transport',
        'mr_lister.connections.entrypoint', 'mr_lister.accounts.provision'))
        for name in sys.modules)
assert all(module.__file__.startswith(archive + '/')
    for name, module in sys.modules.items()
    if name.startswith('mr_lister') and getattr(module, '__file__', None))
assert not any(name.startswith(('mr_lister.publication', 'mr_lister.judge_session',
    'mr_lister.judge_cleanup')) for name in sys.modules)
assert sdk_attempts == []
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(archive), str(source), component],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    if omitted:
        assert result.returncode != 0, "ZIP silently imported a missing dependency from checkout"
    else:
        assert result.returncode == 0, result.stderr
