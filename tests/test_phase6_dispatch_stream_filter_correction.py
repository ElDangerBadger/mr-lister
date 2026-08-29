from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256

import pytest

import tools.render_phase6_dispatch_stream_filter_correction as correction


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode()


def _document(pattern: object) -> dict[str, object]:
    return {
        "Parameters": {"ReleaseFingerprint": {"Default": "a" * 64}},
        "Resources": {
            "DispatcherFunction": {
                "Properties": {
                    "CodeUri": {"Bucket": "exact", "Key": "exact", "Version": "exact"},
                    "Events": {
                        "OperationalStateChanges": {
                            "Properties": {
                                "BatchSize": 25,
                                "BisectBatchOnFunctionError": True,
                                "FilterCriteria": {
                                    "Filters": [
                                        {"Pattern": json.dumps(pattern, separators=(",", ":"))}
                                    ]
                                },
                                "MaximumRetryAttempts": 3,
                                "StartingPosition": "LATEST",
                            },
                            "Type": "DynamoDB",
                        }
                    },
                },
                "Type": "AWS::Serverless::Function",
            },
            "Unchanged": {"Properties": {"Value": "exact"}, "Type": "Custom::Exact"},
        },
    }


def _render(
    monkeypatch: pytest.MonkeyPatch, predecessor: dict[str, object], source: dict[str, object]
) -> bytes:
    predecessor_raw = _canonical(predecessor)
    source_raw = _canonical(source)
    monkeypatch.setattr(
        correction, "PREDECESSOR_TEMPLATE_SHA256", sha256(predecessor_raw).hexdigest()
    )
    monkeypatch.setattr(
        correction, "CORRECTED_SOURCE_TEMPLATE_SHA256", sha256(source_raw).hexdigest()
    )
    expected = deepcopy(predecessor)
    correction._set_pattern(expected, json.dumps(correction.SAFE_FILTER, separators=(",", ":")))
    monkeypatch.setattr(
        correction,
        "DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256",
        sha256(_canonical(expected)).hexdigest(),
    )
    return correction.render_phase6_dispatch_filter_correction(predecessor_raw, source_raw)


def test_render_changes_only_exact_filter_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor = _document(correction.OLD_FILTER)
    source = _document(correction.SAFE_FILTER)
    rendered = _render(monkeypatch, predecessor, source)
    target = json.loads(rendered)

    assert correction._strict_pattern(correction._pattern(target)) == correction.SAFE_FILTER
    assert correction._changed_paths(predecessor, target) == {correction._FILTER_PATH}
    restored = deepcopy(target)
    correction._set_pattern(restored, correction._pattern(predecessor))
    assert restored == predecessor


def test_render_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor = _document(correction.OLD_FILTER)
    source = _document(correction.SAFE_FILTER)
    assert _render(monkeypatch, predecessor, source) == _render(monkeypatch, predecessor, source)


@pytest.mark.parametrize("drift", ("predecessor", "source"))
def test_render_rejects_filter_drift(monkeypatch: pytest.MonkeyPatch, drift: str) -> None:
    predecessor = _document(correction.OLD_FILTER)
    source = _document(correction.SAFE_FILTER)
    target = predecessor if drift == "predecessor" else source
    correction._set_pattern(target, json.dumps({"unexpected": True}, separators=(",", ":")))
    with pytest.raises(correction.Phase6DispatchFilterCorrectionError):
        _render(monkeypatch, predecessor, source)


def test_render_rejects_noncanonical_predecessor(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor = _document(correction.OLD_FILTER)
    source = _document(correction.SAFE_FILTER)
    predecessor_raw = json.dumps(predecessor).encode()
    source_raw = _canonical(source)
    monkeypatch.setattr(
        correction, "PREDECESSOR_TEMPLATE_SHA256", sha256(predecessor_raw).hexdigest()
    )
    monkeypatch.setattr(
        correction, "CORRECTED_SOURCE_TEMPLATE_SHA256", sha256(source_raw).hexdigest()
    )
    with pytest.raises(correction.Phase6DispatchFilterCorrectionError):
        correction.render_phase6_dispatch_filter_correction(predecessor_raw, source_raw)


def test_checked_in_authorities_and_exact_private_output() -> None:
    predecessor = correction.DEFAULT_PREDECESSOR_PATH.read_bytes()
    source = correction.SOURCE_PATH.read_bytes()
    rendered = correction.render_phase6_dispatch_filter_correction(predecessor, source)
    assert sha256(predecessor).hexdigest() == correction.PREDECESSOR_TEMPLATE_SHA256
    assert sha256(source).hexdigest() == correction.CORRECTED_SOURCE_TEMPLATE_SHA256
    assert sha256(rendered).hexdigest() == correction.DISPATCH_FILTER_CORRECTION_TEMPLATE_SHA256


def test_write_is_create_or_identical_and_confined(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "repo"
    predecessor_path = root / ".mr_lister_private/old/template.json"
    source_path = root / "infra/template.json"
    output_path = root / ".mr_lister_private/new/template.json"
    predecessor_path.parent.mkdir(parents=True)
    source_path.parent.mkdir(parents=True)
    predecessor = _document(correction.OLD_FILTER)
    source = _document(correction.SAFE_FILTER)
    predecessor_path.write_bytes(_canonical(predecessor))
    source_path.write_bytes(_canonical(source))
    monkeypatch.setattr(correction, "REPOSITORY_ROOT", root)
    monkeypatch.setattr(correction, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    monkeypatch.setattr(correction, "SOURCE_PATH", source_path)
    monkeypatch.setattr(correction, "DEFAULT_OUTPUT_PATH", output_path)
    expected = _render(monkeypatch, predecessor, source)
    assert correction.write_phase6_dispatch_filter_correction().read_bytes() == expected
    assert correction.write_phase6_dispatch_filter_correction().read_bytes() == expected
    output_path.write_text("drift")
    with pytest.raises(correction.Phase6DispatchFilterCorrectionError):
        correction.write_phase6_dispatch_filter_correction()
