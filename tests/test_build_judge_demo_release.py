"""Judge functions get a closed source graph and the existing checked Linux wheels."""

from tools.build_judge_demo_release import ENTRYPOINTS, source_closure, wheel_inventory


def test_judge_source_closure_contains_both_handlers_without_legacy_or_public_routes():
    sources = source_closure()
    assert set(ENTRYPOINTS).issubset(sources)
    assert "mr_lister.judge_cleanup.provider" in sources
    assert "mr_lister.judge_session.tokens" in sources
    assert "mr_lister.publication.execution_dynamodb" in sources
    assert not any("legacy" in name for name in sources)
    assert not any(name.endswith((".enabled_api", ".api", ".adapter")) for name in sources)
    assert "mr_lister.cloud.phase6_upload_api_entrypoint" not in sources


def test_all_jwt_crypto_wheels_are_pinned_and_cannot_overlap_existing_dependencies():
    wheels = wheel_inventory()
    names = [wheel["name"].lower() for wheel in wheels]
    assert len(names) == len(set(names))
    assert {"pyjwt", "cryptography", "cffi", "pycparser", "pydantic", "boto3"}.issubset(names)
    for wheel in wheels:
        assert len(wheel["sha256"]) == 64
        assert "none-any" in wheel["filename"] or "aarch64" in wheel["filename"]
        assert "macosx" not in wheel["filename"]
