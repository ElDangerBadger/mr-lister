from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_phase6_review_query_runtime_envelope as correction


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode()


def _documents() -> tuple[dict[str, object], dict[str, object]]:
    predecessor = {
        "Globals": {"Function": {"MemorySize": 256}},
        "Metadata": {"ExistingAuthority": {"Mode": "active-draft-only"}},
        "Resources": {
            "ReviewQueryApiFunction": {
                "Properties": {"Handler": "phase6_lambda.review_query_api_handler", "Timeout": 15},
                "Type": "AWS::Serverless::Function",
            },
            "UnchangedResource": {"Properties": {"Value": "exact"}, "Type": "Custom::Exact"},
        },
    }
    source = {
        "Resources": {
            "ReviewQueryApiFunction": {
                "Properties": {"MemorySize": 512, "Timeout": 30},
                "Type": "AWS::Serverless::Function",
            }
        }
    }
    return predecessor, source


def _render(
    monkeypatch: pytest.MonkeyPatch,
    predecessor: dict[str, object],
    source: dict[str, object],
) -> dict[str, object]:
    predecessor_raw = _canonical(predecessor)
    source_raw = _canonical(source)
    expected = deepcopy(predecessor)
    properties = expected["Resources"]["ReviewQueryApiFunction"]["Properties"]
    properties["MemorySize"] = 512
    properties["Timeout"] = 30
    expected["Metadata"][correction._METADATA_KEY] = {
        "Changes": {
            "MemorySize": {"From": 256, "To": 512},
            "Timeout": {"From": 15, "To": 30},
        },
        "Format": correction.REVIEW_QUERY_RUNTIME_ENVELOPE_FORMAT,
        "PredecessorTemplateSha256": sha256(predecessor_raw).hexdigest(),
        "Resource": "ReviewQueryApiFunction",
        "SourceTemplateSha256": sha256(source_raw).hexdigest(),
    }
    monkeypatch.setattr(
        correction,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    monkeypatch.setattr(correction, "SOURCE_TEMPLATE_SHA256", sha256(source_raw).hexdigest())
    monkeypatch.setattr(
        correction,
        "REVIEW_QUERY_RUNTIME_ENVELOPE_TEMPLATE_SHA256",
        sha256(_canonical(expected)).hexdigest(),
    )
    return json.loads(
        correction.render_phase6_review_query_runtime_envelope(predecessor_raw, source_raw)
    )


def test_render_changes_only_the_review_query_runtime_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor, source = _documents()

    target = _render(monkeypatch, predecessor, source)

    assert target["Resources"]["ReviewQueryApiFunction"]["Properties"]["MemorySize"] == 512
    assert target["Resources"]["ReviewQueryApiFunction"]["Properties"]["Timeout"] == 30
    assert target["Resources"]["UnchangedResource"] == predecessor["Resources"]["UnchangedResource"]
    assert target["Metadata"][correction._METADATA_KEY]["Format"] == (
        correction.REVIEW_QUERY_RUNTIME_ENVELOPE_FORMAT
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "default_memory",
        "predecessor_memory",
        "predecessor_timeout",
        "source_memory",
        "source_timeout",
    ),
)
def test_render_rejects_drifted_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    predecessor, source = _documents()
    if mutation == "default_memory":
        predecessor["Globals"]["Function"]["MemorySize"] = 128
    elif mutation == "predecessor_memory":
        predecessor["Resources"]["ReviewQueryApiFunction"]["Properties"]["MemorySize"] = 256
    elif mutation == "predecessor_timeout":
        predecessor["Resources"]["ReviewQueryApiFunction"]["Properties"]["Timeout"] = 14
    elif mutation == "source_memory":
        source["Resources"]["ReviewQueryApiFunction"]["Properties"]["MemorySize"] = 256
    else:
        source["Resources"]["ReviewQueryApiFunction"]["Properties"]["Timeout"] = 29

    with pytest.raises(correction.Phase6ReviewQueryRuntimeEnvelopeError):
        _render(monkeypatch, predecessor, source)


def test_deployed_target_constant_is_the_documented_post_web_correction() -> None:
    assert correction.PREDECESSOR_TEMPLATE_SHA256 == (
        "74560fb066f66759f5baa8a3be15c6370e20bfa884a50e0b4b7e0457592ebff4"
    )
    assert correction.REVIEW_QUERY_RUNTIME_ENVELOPE_TEMPLATE_SHA256 == (
        "618fbca8d00b1edbfa7412668a6e7d2a0e4e65e23460ee8b9216f92f19dbdfc2"
    )
    assert sha256(correction.DEFAULT_SOURCE_PATH.read_bytes()).hexdigest() == (
        correction.SOURCE_TEMPLATE_SHA256
    )


def _bind_private_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, bytes]:
    predecessor, source = _documents()
    target = _render(monkeypatch, predecessor, source)
    repository = tmp_path / "repository"
    predecessor_root = repository / "predecessor-input"
    source_root = repository / "source-input"
    predecessor_root.mkdir(parents=True)
    source_root.mkdir(parents=True)
    predecessor_path = predecessor_root / "predecessor.json"
    source_path = source_root / "source.json"
    predecessor_path.write_bytes(_canonical(predecessor))
    source_path.write_bytes(_canonical(source))
    output_path = repository / ".mr_lister_private/correction/target.json"
    monkeypatch.setattr(correction, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(correction, "DEFAULT_PREDECESSOR_PATH", predecessor_path)
    monkeypatch.setattr(correction, "DEFAULT_SOURCE_PATH", source_path)
    monkeypatch.setattr(correction, "DEFAULT_OUTPUT_PATH", output_path)
    return output_path, _canonical(target)


def test_private_write_is_create_or_identical_and_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, expected = _bind_private_write(monkeypatch, tmp_path)

    assert correction.write_phase6_review_query_runtime_envelope() == output_path
    assert output_path.read_bytes() == expected
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.parent.stat().st_mode) == 0o700
    assert correction.write_phase6_review_query_runtime_envelope() == output_path

    output_path.write_bytes(b"different\n")
    with pytest.raises(correction.Phase6ReviewQueryRuntimeEnvelopeError):
        correction.write_phase6_review_query_runtime_envelope()


def test_private_write_rejects_symlinked_output_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    repository = correction.REPOSITORY_ROOT
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (repository / ".mr_lister_private").symlink_to(outside, target_is_directory=True)

    with pytest.raises(correction.Phase6ReviewQueryRuntimeEnvelopeError):
        correction.write_phase6_review_query_runtime_envelope()
    assert not (outside / "correction/target.json").exists()
    assert not output_path.exists()


def test_private_write_rejects_symlinked_input_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    outside = tmp_path / "outside-input"
    outside.mkdir()
    external_predecessor = outside / "predecessor.json"
    external_predecessor.write_bytes(correction.DEFAULT_PREDECESSOR_PATH.read_bytes())
    correction.DEFAULT_PREDECESSOR_PATH.unlink()
    correction.DEFAULT_PREDECESSOR_PATH.parent.rmdir()
    correction.DEFAULT_PREDECESSOR_PATH.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(correction.Phase6ReviewQueryRuntimeEnvelopeError):
        correction.write_phase6_review_query_runtime_envelope()
    assert not output_path.exists()


def test_render_rejects_noncanonical_or_duplicate_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source = _documents()
    source_raw = _canonical(source)
    malformed_values = (
        json.dumps(_documents()[0], sort_keys=True, separators=(",", ":")).encode(),
        b'{"Globals":{},"Globals":{},"Metadata":{},"Resources":{}}',
    )
    monkeypatch.setattr(correction, "SOURCE_TEMPLATE_SHA256", sha256(source_raw).hexdigest())
    for predecessor_raw in malformed_values:
        monkeypatch.setattr(
            correction,
            "PREDECESSOR_TEMPLATE_SHA256",
            sha256(predecessor_raw).hexdigest(),
        )
        with pytest.raises(correction.Phase6ReviewQueryRuntimeEnvelopeError):
            correction.render_phase6_review_query_runtime_envelope(
                predecessor_raw,
                source_raw,
            )


def test_render_rejects_unsealed_target_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor, source = _documents()
    predecessor_raw = _canonical(predecessor)
    source_raw = _canonical(source)
    monkeypatch.setattr(
        correction,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    monkeypatch.setattr(correction, "SOURCE_TEMPLATE_SHA256", sha256(source_raw).hexdigest())
    monkeypatch.setattr(correction, "REVIEW_QUERY_RUNTIME_ENVELOPE_TEMPLATE_SHA256", "0" * 64)

    with pytest.raises(correction.Phase6ReviewQueryRuntimeEnvelopeError):
        correction.render_phase6_review_query_runtime_envelope(predecessor_raw, source_raw)
