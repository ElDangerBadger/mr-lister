"""No-network independent-artwork evaluation, including original JPEG byte integrity."""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from mr_lister.intelligence.harness_candidate import (
    EvidenceBrief,
    HarnessCandidateAdapter,
    HarnessResult,
    VerifiedProductContext,
)
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import InvalidArtworkError
from tools import evaluate_harness_artwork as tool


def image_bytes(format="PNG"):
    data = BytesIO()
    Image.new("RGB", (40, 30), (90, 120, 200)).save(data, format=format)
    return data.getvalue()


@pytest.fixture
def settings():
    return BedrockSettings.model_validate_json(tool.CONFIG.read_text())


def result_for(artwork):
    return HarnessResult(
        state="review_required",
        artwork_sha256=artwork.content_sha256,
        brief=EvidenceBrief(
            observable_features=("Unresolved visible shapes.",),
            supported_subject=None,
            subject_status="uncertain",
        ),
        listing=None,
        subject_verification="unresolved",
        issues=("Review the source.",),
        prompt_version="test",
        prompt_fingerprint="a" * 64,
    )


@pytest.mark.parametrize(
    ("format", "name", "mime"),
    [
        ("PNG", "artwork.png", "image/png"),
        ("JPEG", "artwork.jpg", "image/jpeg"),
    ],
)
def test_source_bytes_media_type_and_hash_stay_intact(tmp_path, format, name, mime):
    content = image_bytes(format)
    path = tmp_path / "subject-secret-from-user.bin"
    path.write_bytes(content)
    artwork, loaded = tool.load_artwork(path)
    assert loaded == content == path.read_bytes()
    assert artwork.filename == name
    assert artwork.content_type == mime
    assert artwork.content_sha256 == sha256(content).hexdigest()
    assert artwork.size_bytes == len(content)


@pytest.mark.parametrize("content", [b"", b"not-image", b"\xff\xd8\xffbroken", image_bytes()[:32]])
def test_invalid_image_rejected_before_model(content):
    with pytest.raises(InvalidArtworkError):
        tool.validate_independent_artwork(content)


def test_current_five_mib_ceiling_rejects_oversize_before_decode(tmp_path, monkeypatch):
    path = tmp_path / "oversize.png"
    with path.open("wb") as handle:
        handle.truncate(tool.MAX_SOURCE_BYTES + 1)
    monkeypatch.setattr(
        tool, "validate_independent_artwork", lambda _: pytest.fail("decoded oversize")
    )
    with pytest.raises(ValueError, match="5 MiB"):
        tool.load_artwork(path)


@pytest.mark.parametrize("trials", [0, 4, -1, True])
def test_trials_bounded(trials):
    with pytest.raises(ValueError):
        tool.validate_options(revision="v2", trials=trials, run_id="run")


def test_run_id_and_revision_fail_before_model():
    for revision, run_id in [("v7", "run"), ("v2", "../escape")]:
        with pytest.raises(ValueError):
            tool.validate_options(revision=revision, trials=1, run_id=run_id)


@pytest.mark.parametrize("revision", [None, "v1", "v2", "v3", "v4", "v5", "v6"])
def test_default_plan_no_aws_no_fixtures_no_source_name(tmp_path, monkeypatch, capsys, revision):
    import boto3

    from tools import evaluate_harness_candidate as shared

    source = tmp_path / "seahorse-secret-name.jpg"
    content = image_bytes("JPEG")
    source.write_bytes(content)
    monkeypatch.setattr(boto3, "Session", lambda **_: pytest.fail("offline plan used AWS"))
    monkeypatch.setattr(shared, "load_manifest", lambda *_: pytest.fail("loaded fixture gold"))
    monkeypatch.setattr(
        shared, "load_verified_briefs", lambda *_: pytest.fail("loaded verified gold")
    )
    args = ["--artwork", str(source)]
    if revision is not None:
        args.extend(["--revision", revision])
    assert tool.main(args) == 0
    output = capsys.readouterr().out
    plan = json.loads(output)
    assert plan["revision"] == plan["fingerprints"]["revision"] == (revision or "v2")
    assert plan["live"] is False
    assert plan["artwork_sha256"] == sha256(content).hexdigest()
    assert plan["artwork_content_type"] == "image/jpeg"
    assert plan["source_path_kind"] == "direct_jpeg_diagnostic"
    assert "seahorse-secret-name" not in output
    assert "manifest" not in output
    assert "verified_briefs" not in output
    assert plan["fingerprints"]["provider_schema_sha256"]
    assert plan["promotion_allowed"] is False


def test_source_binding_and_private_copy_do_not_change_original(tmp_path, settings):
    content = image_bytes("JPEG")
    artwork = tool.validate_independent_artwork(content)
    calls = []

    def prepare(bound_artwork, source):
        calls.append((bound_artwork, source))
        return result_for(bound_artwork)

    output = tool.create_private_run(tmp_path / "private", "independent")
    rows = tool.run_artwork(
        artwork=artwork,
        content=content,
        revision="v2",
        trials=2,
        settings=settings,
        fingerprints={},
        output=output,
        adapter_factory=lambda _: SimpleNamespace(prepare=prepare),
    )
    assert len(calls) == 2
    assert all(source == content and bound == artwork for bound, source in calls)
    assert (output / "artwork.jpg").read_bytes() == content
    assert (output / "artwork.jpg").stat().st_mode & 0o777 == 0o600
    assert (output / "review-pack.json").stat().st_mode & 0o777 == 0o600
    assert all(row["contract_valid"] is True for row in rows)
    assert all(row["listing_available"] is False for row in rows)
    assert all(row["promotion_allowed"] is False for row in rows)
    assert all(row["manual_review"]["status"] == "pending" for row in rows)
    assert all(row["source_path_kind"] == "direct_jpeg_diagnostic" for row in rows)
    assert "legacy_quality_failures" not in rows[0]
    assert "literal_signals" not in rows[0]
    assert "case_id" not in rows[0]


def test_tampered_source_binding_fails_before_adapter(tmp_path, settings):
    content = image_bytes()
    artwork = tool.validate_independent_artwork(content).model_copy(
        update={"content_sha256": "a" * 64}
    )
    output = tool.create_private_run(tmp_path / "private", "tamper")
    with pytest.raises(ValueError):
        tool.run_artwork(
            artwork=artwork,
            content=content,
            revision="v2",
            trials=1,
            settings=settings,
            fingerprints={},
            output=output,
            adapter_factory=lambda _: pytest.fail("tampered source reached adapter"),
        )


def test_frozen_harness_derives_png_from_jpeg_without_modifying_source(settings):
    content = image_bytes("JPEG")
    artwork = tool.validate_independent_artwork(content)
    requests = []

    class Client:
        def complete(self, request):
            requests.append(request)
            brief = {
                "observable_features": ["Unresolved visible shapes."],
                "visible_text": [],
                "supported_subject": None,
                "subject_status": "uncertain",
                "unresolved_alternatives": [],
                "listing_details": [],
            }
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(brief)},
                    }
                ]
            }

    adapter = HarnessCandidateAdapter(
        client=Client(), settings=settings, product_context=VerifiedProductContext(), revision="v2"
    )
    result = adapter.prepare(artwork, content)
    assert result.artwork_sha256 == sha256(content).hexdigest()
    assert result.state == "review_required"
    image_blocks = [
        block
        for message in requests[0]["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "image_url"
    ]
    uri = image_blocks[0]["image_url"]["url"]
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]).startswith(b"\x89PNG")
    assert content.startswith(b"\xff\xd8\xff")
    assert artwork.content_type == "image/jpeg"


def test_error_text_is_redacted_and_no_result_contract_is_unknown(tmp_path, settings):
    content = image_bytes()
    artwork = tool.validate_independent_artwork(content)

    def fail(*_):
        raise RuntimeError("private echoed provider value")

    output = tool.create_private_run(tmp_path / "private", "failure")
    rows = tool.run_artwork(
        artwork=artwork,
        content=content,
        revision="v2",
        trials=1,
        settings=settings,
        fingerprints={},
        output=output,
        adapter_factory=lambda _: SimpleNamespace(prepare=fail),
    )
    text = (output / "review-pack.json").read_text()
    assert "RuntimeError" in text
    assert "private echoed provider value" not in text
    assert rows[0]["contract_valid"] is None


@pytest.mark.parametrize("revision", ["v1", "v2", "v3", "v4", "v5", "v6"])
def test_live_flag_still_requires_environment_opt_ins(tmp_path, monkeypatch, revision):
    import boto3

    source = tmp_path / "test.png"
    source.write_bytes(image_bytes())
    monkeypatch.delenv("MR_LISTER_RUN_LIVE_BEDROCK", raising=False)
    monkeypatch.setattr(boto3, "Session", lambda **_: pytest.fail("unauthorized session"))
    with pytest.raises(ValueError, match="MR_LISTER_RUN_LIVE_BEDROCK"):
        tool.main(["--live", "--artwork", str(source), "--revision", revision])


@pytest.mark.parametrize("revision", ["v1", "v2", "v3", "v4", "v5", "v6"])
def test_live_cli_binds_revision_and_private_artifacts_without_network(
    tmp_path, monkeypatch, revision
):
    import boto3

    from mr_lister.intelligence import harness_candidate as candidate
    from tools.evaluate_harness_candidate import EXPECTED_ACCOUNT_ID

    content = image_bytes("JPEG")
    source = tmp_path / "user-subject-label.jpg"
    source.write_bytes(content)
    calls = []

    def builder(settings, *, session, diagnostics, revision):
        def prepare(artwork, snapshot):
            calls.append((revision, artwork, snapshot))
            return result_for(artwork)

        return SimpleNamespace(prepare=prepare)

    session = SimpleNamespace(
        client=lambda name: SimpleNamespace(
            get_caller_identity=lambda: {
                "Account": EXPECTED_ACCOUNT_ID,
                "Arn": f"arn:aws:iam::{EXPECTED_ACCOUNT_ID}:user/mr-lister-dev",
            }
        )
    )
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: session)
    monkeypatch.setattr(candidate, "build_harness_candidate_adapter", builder)
    monkeypatch.setattr(tool, "REPO_ROOT", tmp_path)
    for key in ("MR_LISTER_RUN_LIVE_BEDROCK", "MR_LISTER_RUN_HARNESS_EVAL"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("AWS_PROFILE", "mr-lister-dev")
    assert (
        tool.main(
            ["--live", "--artwork", str(source), "--revision", revision, "--run-id", "fake-live"]
        )
        == 0
    )
    assert len(calls) == 1
    actual_revision, artwork, snapshot = calls[0]
    assert actual_revision == revision
    assert snapshot == source.read_bytes() == content
    assert artwork.filename == "artwork.jpg"
    output = tmp_path / ".mr_lister_private/artwork-experiments/fake-live"
    plan = json.loads((output / "plan.json").read_text())
    row = json.loads((output / "trial-1.json").read_text())
    assert plan["revision"] == row["revision"] == revision
    assert plan["fingerprints"]["revision"] == row["fingerprints"]["revision"] == revision
    assert row["fingerprints"] == plan["fingerprints"]
    assert (output / "artwork.jpg").read_bytes() == content
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert row["commerce_writes"] == 0
    assert row["manual_review"]["status"] == "pending"
    assert row["promotion_allowed"] is False
