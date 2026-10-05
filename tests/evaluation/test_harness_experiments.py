"""Offline tests of harness experiment gates, source isolation and review artifacts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mr_lister.intelligence.harness_candidate import EvidenceBrief, HarnessResult
from mr_lister.intelligence.settings import BedrockSettings
from tools import evaluate_harness_candidate as harness
from tools.phase2_evaluation import load_manifest


@pytest.fixture
def cases():
    return load_manifest(harness.MANIFEST).cases


@pytest.fixture
def settings():
    return BedrockSettings.model_validate_json(harness.CONFIG.read_text())


def test_all_verified_briefs_bound_to_original_images_and_accurate_provenance(cases):
    verified = harness.load_verified_briefs(harness.BRIEFS, cases)
    assert len(verified) == 11
    assert all(row.review_source == "assistant_reviewed_fixture_facts" for row in verified.values())
    assert verified["typography_maker_motto"].brief.visible_text == ("MAKE GOOD THINGS",)
    assert "wrench" in " ".join(verified["typography_maker_motto"].brief.observable_features)
    assert (
        verified["holdout_v6_transparent_seahorse"].brief.supported_subject == "stylized seahorse"
    )


@pytest.mark.parametrize("change", ["checksum", "provenance", "duplicate"])
def test_invalid_verified_reference_fails_before_inference(tmp_path, cases, change):
    payload = json.loads(harness.BRIEFS.read_text())
    if change == "checksum":
        payload["cases"][0]["artwork_sha256"] = "0" * 64
    elif change == "provenance":
        payload["cases"][0]["review_source"] = "human"
    else:
        payload["cases"].append(payload["cases"][0])
    path = tmp_path / "briefs.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        harness.load_verified_briefs(path, cases)


@pytest.mark.parametrize("trials", [0, 4, -1])
def test_experiment_trials_are_bounded(cases, trials):
    with pytest.raises(ValueError):
        harness.select_cases(cases, [], all_cases=True, trials=trials)


def test_case_selection_rejects_unknown_duplicates_and_ambiguous_all(cases):
    for names, all_cases in [
        (["unknown"], False),
        ([cases[0].case_id] * 2, False),
        ([cases[0].case_id], True),
    ]:
        with pytest.raises(ValueError):
            harness.select_cases(cases, names, all_cases=all_cases, trials=1)
    assert harness.select_cases(cases, [], all_cases=False, trials=1) == (cases[0],)


def test_live_access_requires_all_opt_ins_and_dev_profile():
    env = {
        "MR_LISTER_RUN_LIVE_BEDROCK": "1",
        "MR_LISTER_RUN_HARNESS_EVAL": "1",
        "MR_LISTER_RUN_FULL_BEDROCK_EVAL": "1",
        "AWS_PROFILE": "mr-lister-dev",
    }
    harness.validate_live_opt_in(env, 3)
    for field in env:
        with pytest.raises(ValueError):
            harness.validate_live_opt_in({k: v for k, v in env.items() if k != field}, 3)
    with pytest.raises(ValueError):
        harness.validate_live_opt_in({**env, "AWS_PROFILE": "mr-lister-bootstrap"}, 1)


@pytest.mark.parametrize("arn", ["root", "role/mr-lister-dev", "user/other"])
def test_identity_gate_rejects_root_roles_and_other_users(arn):
    with pytest.raises(ValueError):
        harness.validate_identity(
            {
                "Account": harness.EXPECTED_ACCOUNT_ID,
                "Arn": f"arn:aws:iam::{harness.EXPECTED_ACCOUNT_ID}:{arn}",
            }
        )
    harness.validate_identity(
        {
            "Account": harness.EXPECTED_ACCOUNT_ID,
            "Arn": f"arn:aws:iam::{harness.EXPECTED_ACCOUNT_ID}:user/mr-lister-dev",
        }
    )


def test_provider_latency_remains_unknown_and_wall_time_is_measured():
    records = [
        {"latency_ms": None, "status": "success", "usage": {"inputTokens": 10, "outputTokens": 2}},
        {
            "latency_ms": 9,
            "status": "invalid_output",
            "usage": {"inputTokens": 7, "outputTokens": 3},
        },
    ]
    summary = harness.summarize_telemetry(records, 1234.25)
    assert summary == {
        "wall_clock_ms": 1234.25,
        "provider_latency_ms": None,
        "invocation_attempts": 2,
        "repair_attempts": None,
        "repair_attempts_by_operation": None,
        "invalid_output_count": 1,
        "input_tokens": 17,
        "output_tokens": 5,
        "total_tokens": 22,
    }
    assert harness.summarize_telemetry([], 1)["input_tokens"] is None


def test_artifacts_are_private_exclusive_and_reject_path_escape(tmp_path):
    output = harness.create_private_run(tmp_path / "private", "run1")
    harness.write_private(output / "output.json", "{}")
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "output.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        harness.create_private_run(tmp_path / "private", "run1")
    with pytest.raises(ValueError):
        harness.create_private_run(tmp_path / "private", "../escape")
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "private")
    with pytest.raises(ValueError):
        harness.create_private_run(link, "run2")


def result_for(artwork, brief):
    return HarnessResult(
        state="review_required",
        artwork_sha256=artwork.content_sha256,
        brief=brief,
        listing=None,
        issues=("Offline fixture; manual review needed.",),
        subject_verification="unresolved",
        prompt_version="test",
        prompt_fingerprint="a" * 64,
    )


def test_writer_ab_uses_identical_verified_facts_without_images(tmp_path, cases, settings):
    verified = harness.load_verified_briefs(harness.BRIEFS, cases)
    calls = []

    class FakeAdapter:
        def write_verified(self, artwork, brief, *, content, prompt_variant):
            calls.append((artwork, brief, content, prompt_variant))
            return result_for(artwork, brief.brief)

        def prepare(self, *_):
            pytest.fail("Writer-only experiment must never invoke vision")

    output = harness.create_private_run(tmp_path / "private", "writer")
    records = harness.run_experiment(
        mode="writer-ab",
        cases=cases[:2],
        trials=1,
        verified_briefs=verified,
        adapter_factory=lambda sink: FakeAdapter(),
        output=output,
        fingerprints={},
        settings=settings,
    )
    assert [call[3] for call in calls] == ["current", "candidate", "candidate", "current"]
    assert calls[0][1] is calls[1][1]
    assert all(call[2] is None and call[0].filename == "artwork.png" for call in calls)
    assert records[0]["verified_brief_sha256"] == records[1]["verified_brief_sha256"]
    assert all(row["promotion_allowed"] is False for row in records)
    assert all(row["outcome"] == "review_required" for row in records)
    assert all(row["contract_valid"] is True for row in records)
    assert all(row["listing_available"] is False for row in records)
    assert all(row["literal_signals"] is None for row in records)
    assert all(
        row["legacy_quality_status"] == "not_assessed_no_accepted_listing" for row in records
    )
    assert all(row["legacy_quality_failures"] is None for row in records)
    assert all(row["manual_review"]["status"] == "pending" for row in records)
    assert (output / "review-pack.json").exists()
    assert "Copy has not been approved by the user" in (output / "review-pack.md").read_text()


def test_full_run_does_not_read_or_send_verified_answers(tmp_path, cases, settings):
    class ForbiddenBriefs:
        def __getitem__(self, _):
            pytest.fail("Full vision must not access gold briefs")

    calls = []

    class FakeAdapter:
        def prepare(self, artwork, content):
            calls.append((artwork, content))
            brief = EvidenceBrief(
                observable_features=("Model-derived circles and lines.",),
                supported_subject=None,
                subject_status="uncertain",
            )
            return result_for(artwork, brief)

        def write_verified(self, *_args, **_kwargs):
            pytest.fail("Full mode must use image-derived evidence")

    output = harness.create_private_run(tmp_path / "private", "full")
    records = harness.run_experiment(
        mode="full",
        cases=cases[-1:],
        trials=1,
        verified_briefs=ForbiddenBriefs(),
        adapter_factory=lambda sink: FakeAdapter(),
        output=output,
        fingerprints={},
        settings=settings,
    )
    assert calls[0][0].filename == "artwork.png"
    assert calls[0][1] == cases[-1].asset.read_bytes()
    assert records[0]["verified_brief_sha256"] is None
    assert records[0]["provenance"] == "model_image_observation"


def test_provider_error_text_is_not_copied_to_artifacts(tmp_path, cases, settings):
    def failed(*_args, **_kwargs):
        raise RuntimeError("secret-provider-request-value")

    output = harness.create_private_run(tmp_path / "private", "failure")
    harness.run_experiment(
        mode="full",
        cases=cases[:1],
        trials=1,
        verified_briefs={},
        adapter_factory=lambda sink: SimpleNamespace(prepare=failed),
        output=output,
        fingerprints={},
        settings=settings,
    )
    text = (output / "review-pack.json").read_text()
    assert "RuntimeError" in text
    assert "secret-provider-request-value" not in text


def test_default_plan_does_not_create_aws_session(monkeypatch, capsys):
    import boto3

    monkeypatch.setattr(
        boto3, "Session", lambda *_args, **_kwargs: pytest.fail("Offline plan made an AWS session")
    )
    assert harness.main(["--mode", "writer-ab"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["revision"] == plan["fingerprints"]["revision"] == "v1"
    assert plan["live"] is False
    assert plan["commerce_writes"] == 0
    assert plan["promotion_allowed"] is False
    assert plan["fingerprints"]["application_schema_sha256"]["EvidenceBrief"]


def test_accepted_model_result_still_requires_manual_semantic_review(
    tmp_path, cases, settings, listing
):
    verified = harness.load_verified_briefs(harness.BRIEFS, cases)

    class AcceptedAdapter:
        def prepare(self, artwork, _content):
            return HarnessResult(
                state="accepted_for_evaluation",
                artwork_sha256=artwork.content_sha256,
                brief=verified[cases[0].case_id].brief,
                listing=listing,
                subject_verification="agrees",
                prompt_version="test",
                prompt_fingerprint="a" * 64,
            )

    output = harness.create_private_run(tmp_path / "private", "accepted")
    rows = harness.run_experiment(
        mode="full",
        cases=cases[:1],
        trials=1,
        verified_briefs={},
        adapter_factory=lambda sink: AcceptedAdapter(),
        output=output,
        fingerprints={},
        settings=settings,
    )
    assert rows[0]["result"]["state"] == "accepted_for_evaluation"
    assert rows[0]["contract_valid"] is True
    assert rows[0]["listing_available"] is True
    assert rows[0]["legacy_quality_status"] == "assessed"
    assert rows[0]["manual_review"]["status"] == "pending"
    assert rows[0]["manual_review"]["critical_subject_error"] is None
    assert rows[0]["promotion_allowed"] is False
    assert "legacy_quality_failures" in rows[0]


@pytest.mark.parametrize(
    ("attempts", "repairs", "invalid_outputs"),
    [
        ([("inspect", 1, "invalid_output")], 0, 1),
        ([("inspect", 1, "invalid_output"), ("inspect", 2, "accepted")], 1, 1),
        ([("inspect", 1, "invalid_output"), ("inspect", 2, "invalid_output")], 1, 2),
        (
            [
                ("inspect", 1, "invalid_output"),
                ("inspect", 2, "accepted"),
                ("write", 1, "invalid_output"),
                ("write", 2, "invalid_output"),
            ],
            2,
            3,
        ),
        ([("write", 1, "invalid_output"), ("write", 2, "provider_error")], 1, 1),
    ],
)
def test_repairs_count_extra_attempts_not_invalid_responses(attempts, repairs, invalid_outputs):
    records = [
        {"operation": operation, "attempt": attempt, "status": status}
        for operation, attempt, status in attempts
    ]
    telemetry = harness.summarize_telemetry(records, 123)
    assert telemetry["repair_attempts"] == repairs
    assert telemetry["invalid_output_count"] == invalid_outputs
    assert telemetry["invocation_attempts"] == len(attempts)
    assert "model_calls" not in telemetry
    assert sum(telemetry["repair_attempts_by_operation"].values()) == repairs


@pytest.mark.parametrize("attempt", [None, 0, -1, True, "2"])
def test_unknown_attempt_metadata_is_not_inferred_from_invalid_outputs(attempt):
    telemetry = harness.summarize_telemetry(
        [{"operation": "write", "attempt": attempt, "status": "invalid_output"}], 123
    )
    assert telemetry["repair_attempts"] is None
    assert telemetry["repair_attempts_by_operation"] is None
    assert telemetry["invalid_output_count"] == 1


@pytest.mark.parametrize("revision", ["v2", "v3", "v4"])
def test_revision_fingerprints_bind_evaluator_source_and_preserve_reference(settings, revision):
    from hashlib import sha256
    from pathlib import Path

    first = harness.experiment_fingerprints(settings)
    second = harness.experiment_fingerprints(settings, revision=revision)
    assert first["revision"] == "v1"
    assert second["revision"] == revision
    assert (
        first["evaluator_source_sha256"] == sha256(Path(harness.__file__).read_bytes()).hexdigest()
    )
    assert first["current_prompt_fingerprint"] == second["current_prompt_fingerprint"]
    assert first["application_schema_sha256"] == second["application_schema_sha256"]
    assert first["provider_schema_sha256"] == second["provider_schema_sha256"]
    assert first["verified_briefs_sha256"] == second["verified_briefs_sha256"]
    assert first["candidate_prompts"] != second["candidate_prompts"]


@pytest.mark.parametrize("revision", ["v1", "v2", "v3", "v4"])
@pytest.mark.parametrize("mode", ["writer-ab", "full"])
def test_live_cli_passes_revision_to_adapter_without_network(
    monkeypatch, tmp_path, cases, revision, mode
):
    import boto3

    from mr_lister.intelligence import harness_candidate as candidate

    revisions = []
    verified = harness.load_verified_briefs(harness.BRIEFS, cases)

    class FakeAdapter:
        def write_verified(self, artwork, brief, **kwargs):
            return result_for(artwork, brief.brief)

        def prepare(self, artwork, content):
            assert content == cases[0].asset.read_bytes()
            assert artwork.filename == "artwork.png"
            return result_for(artwork, verified[cases[0].case_id].brief)

    def fake_builder(_settings, *, session, diagnostics, revision):
        revisions.append(revision)
        return FakeAdapter()

    session = SimpleNamespace(
        client=lambda name: SimpleNamespace(
            get_caller_identity=lambda: {
                "Account": harness.EXPECTED_ACCOUNT_ID,
                "Arn": f"arn:aws:iam::{harness.EXPECTED_ACCOUNT_ID}:user/mr-lister-dev",
            }
        )
    )
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: session)
    monkeypatch.setattr(candidate, "build_harness_candidate_adapter", fake_builder)
    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)
    for key in ("MR_LISTER_RUN_LIVE_BEDROCK", "MR_LISTER_RUN_HARNESS_EVAL"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("AWS_PROFILE", "mr-lister-dev")
    assert harness.main(
        ["--live", "--mode", mode, "--revision", revision, "--run-id", "offline-fake"]
    ) == 0
    assert revisions == [revision] * (2 if mode == "writer-ab" else 1)
    output = tmp_path / ".mr_lister_private/harness-experiments/offline-fake"
    plan = json.loads((output / "plan.json").read_text())
    rows = json.loads((output / "review-pack.json").read_text())["records"]
    assert plan["revision"] == plan["fingerprints"]["revision"] == revision
    assert all(row["revision"] == revision for row in rows)
    assert all(row["fingerprints"]["evaluator_source_sha256"] for row in rows)
    assert all(row["manual_review"]["status"] == "pending" for row in rows)
    assert all(row["promotion_allowed"] is False for row in rows)
    if mode == "writer-ab":
        assert rows[0]["verified_brief_sha256"] == harness.digest(
            verified[cases[0].case_id].model_dump(mode="json")
        )
        assert (output / "paired-review.md").exists()
        assert (output / "paired-reveal.json").stat().st_mode & 0o777 == 0o600
    else:
        assert rows[0]["verified_brief_sha256"] is None
        assert rows[0]["provenance"] == "model_image_observation"
        assert rows[0]["input_image_sent"] is True


def blind_records(cases):
    return [
        {
            "case_id": case.case_id,
            "trial": 1,
            "mode": "writer-ab",
            "arm": arm,
            "revision": "v2",
            "verified_brief_sha256": case.asset_sha256,
            "artwork_sha256": case.asset_sha256,
            "listing_available": True,
            "result": {
                "listing": {
                    "title": f"Private title {index}",
                    "description": "Private description",
                    "tags": ["sample tag"],
                }
            },
            "telemetry": {"wall_clock_ms": 9999},
            "prompt_version": "Private internal marker",
        }
        for case in cases
        for index, arm in enumerate(("current", "candidate"))
    ]


def test_paired_pack_blinds_metadata_and_maps_stable_shuffled_labels(cases):
    rows = blind_records(cases)
    paired, reveal = harness.paired_review_pack(rows, cases)
    assert (paired, reveal) == harness.paired_review_pack(list(reversed(rows)), cases)
    serialized = json.dumps(paired)
    assert '"arm"' not in serialized
    assert '"telemetry"' not in serialized
    assert "9999" not in serialized
    assert "Private internal marker" not in serialized
    assert "current" not in serialized
    assert "candidate" not in serialized
    assert {pair["labels"]["A"]["arm"] for pair in reveal["pairs"]} == {"current", "candidate"}
    for pair, mapping in zip(paired["pairs"], reveal["pairs"], strict=True):
        assert pair["preference"] is None
        assert pair["pair_id"] == mapping["pair_id"]
        for label in ("A", "B"):
            arm = mapping["labels"][label]["arm"]
            index = ("current", "candidate").index(arm)
            assert pair["options"][label]["title"] == f"Private title {index}"
            assert pair["options"][label]["review"]["critical_subject_error"] is None
    assert paired["promotion_allowed"] is False


@pytest.mark.parametrize("damage", ["brief", "revision", "missing", "duplicate"])
def test_paired_pack_refuses_noncomparable_pairs(cases, damage):
    rows = blind_records(cases[:1])
    if damage == "brief":
        rows[0]["verified_brief_sha256"] = "b" * 64
    elif damage == "revision":
        rows[0]["revision"] = "v1"
    elif damage == "missing":
        rows.pop()
    else:
        rows.append(rows[0])
    with pytest.raises(ValueError):
        harness.paired_review_pack(rows, cases)


def test_unknown_revision_fails_in_cli_before_network(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "Session", lambda **kwargs: pytest.fail("unexpected AWS session"))
    with pytest.raises(SystemExit):
        harness.main(["--live", "--revision", "v5"])
