"""Offline integrity/budget checks; semantic image quality is evaluated separately."""

from __future__ import annotations

import base64
import json
import random
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image
from test_harness_candidate import ScriptedClient, artwork, draft, evidence, images, response

from mr_lister.intelligence.harness_candidate import (
    HarnessCandidateAdapter,
    VerifiedProductContext,
    candidate_prompt_bundles,
)
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import IntelligenceConfigurationError
from tools import evaluate_harness_detail as detail


def noise_png(side=768):
    buffer = BytesIO()
    Image.frombytes("RGB", (side, side), random.Random(83).randbytes(side * side * 3)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def adapter(client, profile):
    return detail.DetailHarnessCandidateAdapter(
        client=client,
        profile=profile,
        revision="v3",
        settings=BedrockSettings(transport="mantle", model_id="google.gemma-4-31b"),
        product_context=VerifiedProductContext(),
    )


def test_profiles_preserve_source_and_standard_bytes_while_detail_can_retain_more_pixels():
    content = noise_png()
    source = artwork(content)
    standard = detail.prepare_profile(source, content, "standard")
    larger = detail.prepare_profile(source, content, "detail")

    assert standard == HarnessCandidateAdapter._source_image(source, content)
    assert sha256(content).hexdigest() == source.content_sha256
    assert len(standard.content) <= 750_000
    assert len(larger.content) <= 2_300_000
    assert standard.width < larger.width == larger.source_width == 768
    assert standard.height < larger.height == larger.source_height == 768


def test_already_small_renditions_are_identical_without_artificial_upscaling():
    content = noise_png(64)
    source = artwork(content)
    standard = detail.prepare_profile(source, content, "standard")
    larger = detail.prepare_profile(source, content, "detail")

    assert standard == larger
    assert (larger.width, larger.height) == (64, 64)


@pytest.mark.parametrize("profile", ["standard", "detail"])
def test_tampered_source_cannot_reach_inference(profile):
    content = noise_png(64)
    client = ScriptedClient(response(evidence()), response(draft()))
    with pytest.raises(IntelligenceConfigurationError):
        adapter(client, profile).prepare(artwork(content), content + b"altered")
    assert client.calls == []


@pytest.mark.parametrize("profile", ["", "high", "production", None])
def test_unknown_profile_is_rejected(profile):
    content = noise_png(64)
    with pytest.raises((ValueError, IntelligenceConfigurationError)):
        detail.prepare_profile(artwork(content), content, profile)


@pytest.mark.parametrize("profile", ["standard", "detail"])
def test_both_calls_receive_same_bounded_image_and_keep_frozen_prompts(profile):
    content = noise_png()
    raw = ScriptedClient(response(evidence()), response(draft()))
    measured = detail.MeasuringClient(raw)

    result = adapter(measured, profile).prepare(artwork(content), content)

    assert result.state == "accepted_for_evaluation"
    assert result.prompt_fingerprint == candidate_prompt_bundles(revision="v3")["full"].fingerprint
    assert images(raw.calls[0]) == images(raw.calls[1])
    assert len(measured.records) == 2
    for request in raw.calls:
        png = base64.b64decode(images(request)[0].split(",", 1)[1])
        assert len(png) <= (750_000 if profile == "standard" else 2_300_000)
        assert len(json.dumps(request).encode()) <= 3_500_000
    safe = json.dumps(measured.records)
    assert "data:image" not in safe
    assert "badger explorer" not in safe
    assert all(record["elapsed_ms"] >= 0 for record in measured.records)


def test_detail_arm_still_rejects_oversized_repair_history_before_network():
    content = noise_png(850)
    raw = ScriptedClient(
        response(evidence()), response("private response " + "x" * 800_000), response(draft())
    )
    measured = detail.MeasuringClient(raw)

    with pytest.raises(IntelligenceConfigurationError):
        adapter(measured, "detail").prepare(artwork(content), content)

    assert len(raw.calls) == 2
    assert len(raw.responses) == 1
    assert len(measured.records) == 2
    assert "private response" not in json.dumps(measured.records)


def test_measurements_retain_elapsed_time_on_delegate_error_without_error_message():
    raw = ScriptedClient(RuntimeError("sensitive provider text"))
    measured = detail.MeasuringClient(raw)
    with pytest.raises(RuntimeError):
        measured.complete({"model": "google.gemma-4-31b", "messages": []})
    assert len(measured.records) == 1
    assert measured.records[0]["elapsed_ms"] >= 0
    assert "sensitive" not in json.dumps(measured.records)


def test_balanced_schedule_reverses_order_for_each_repeat_and_case():
    schedule = detail.trial_schedule(6, 2)
    assert len(schedule) == len(set(schedule)) == 24
    for case in range(6):
        first = [profile for c, trial, profile in schedule if c == case and trial == 1]
        second = [profile for c, trial, profile in schedule if c == case and trial == 2]
        assert set(first) == set(second) == {"standard", "detail"}
        assert first == list(reversed(second))
        if case:
            previous = [p for c, trial, p in schedule if c == case - 1 and trial == 1]
            assert first == list(reversed(previous))


def test_default_cli_is_offline_and_marks_identical_input_control(tmp_path, monkeypatch, capsys):
    import boto3

    image = tmp_path / "do-not-send-this-subject-name.png"
    image.write_bytes(noise_png(64))
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: pytest.fail("Unexpected AWS session"))

    assert detail.main(["--artwork", str(image)]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["live"] is False
    assert plan["cases"][0]["identical_input_control"] is True
    assert plan["cases"][0]["artwork_sha256"] == sha256(image.read_bytes()).hexdigest()
    assert "do-not-send-this-subject-name" not in json.dumps(plan)


def test_cli_requires_live_flags_before_aws_access(tmp_path, monkeypatch):
    import boto3

    for name in ("MR_LISTER_RUN_LIVE_BEDROCK", "MR_LISTER_RUN_HARNESS_EVAL"):
        monkeypatch.delenv(name, raising=False)
    image = tmp_path / "artwork.png"
    image.write_bytes(noise_png(64))
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: pytest.fail("Unexpected AWS session"))
    with pytest.raises(ValueError, match="Live calls require"):
        detail.main(["--artwork", str(image), "--live"])
