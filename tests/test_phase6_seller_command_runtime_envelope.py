from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_phase6_seller_command_runtime_envelope as correction


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
    ).encode()


def _documents() -> tuple[dict[str, object], dict[str, object]]:
    predecessor = {
        "Globals": {"Function": {"MemorySize": 256}},
        "Metadata": {"ExistingAuthority": {"Mode": "active-draft-only"}},
        "Resources": {
            "SellerCommandApiFunction": {
                "Properties": {
                    "Handler": "phase6_lambda.seller_command_api_handler",
                    "Timeout": 30,
                },
                "Type": "AWS::Serverless::Function",
            },
            "UnchangedResource": {"Properties": {"Value": "exact"}, "Type": "Custom::Exact"},
        },
    }
    source = {
        "Resources": {
            "SellerCommandApiFunction": {
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
    expected["Resources"]["SellerCommandApiFunction"]["Properties"]["MemorySize"] = 512
    monkeypatch.setattr(
        correction,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    monkeypatch.setattr(correction, "SOURCE_TEMPLATE_SHA256", sha256(source_raw).hexdigest())
    monkeypatch.setattr(
        correction,
        "SELLER_COMMAND_RUNTIME_ENVELOPE_TEMPLATE_SHA256",
        sha256(_canonical(expected)).hexdigest(),
    )
    return json.loads(
        correction.render_phase6_seller_command_runtime_envelope(predecessor_raw, source_raw)
    )


def test_render_changes_only_seller_command_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor, source = _documents()

    target = _render(monkeypatch, predecessor, source)

    properties = target["Resources"]["SellerCommandApiFunction"]["Properties"]
    assert properties["MemorySize"] == 512
    assert properties["Timeout"] == 30
    assert target["Resources"]["UnchangedResource"] == predecessor["Resources"]["UnchangedResource"]
    assert target["Metadata"] == predecessor["Metadata"]
    assert correction._changed_paths(predecessor, target) == {correction._MEMORY_PATH}


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
        predecessor["Resources"]["SellerCommandApiFunction"]["Properties"]["MemorySize"] = 256
    elif mutation == "predecessor_timeout":
        predecessor["Resources"]["SellerCommandApiFunction"]["Properties"]["Timeout"] = 29
    elif mutation == "source_memory":
        source["Resources"]["SellerCommandApiFunction"]["Properties"]["MemorySize"] = 256
    else:
        source["Resources"]["SellerCommandApiFunction"]["Properties"]["Timeout"] = 29

    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        _render(monkeypatch, predecessor, source)


def test_checked_in_authorities_render_exact_target() -> None:
    predecessor_raw = correction.DEFAULT_PREDECESSOR_PATH.read_bytes()
    source_raw = correction.DEFAULT_SOURCE_PATH.read_bytes()

    rendered = correction.render_phase6_seller_command_runtime_envelope(
        predecessor_raw,
        source_raw,
    )

    assert correction.PREDECESSOR_TEMPLATE_SHA256 == (
        "e5fb4ba29915fd8ec4261476987432c20b490f339019f4eb6a6972b4c24f86c3"
    )
    assert correction.SOURCE_TEMPLATE_SHA256 == (
        "1f9772e04ace5a035febeea14b92417866326e5f07a0495a981648dac625fd09"
    )
    assert correction.SELLER_COMMAND_RUNTIME_ENVELOPE_TEMPLATE_SHA256 == (
        "6a6775a01f7c836ba90efb8f0a9259d389daac32b52c5bf553a4752aeb9f8791"
    )
    assert sha256(predecessor_raw).hexdigest() == correction.PREDECESSOR_TEMPLATE_SHA256
    assert sha256(source_raw).hexdigest() == correction.SOURCE_TEMPLATE_SHA256
    assert (
        sha256(rendered).hexdigest() == correction.SELLER_COMMAND_RUNTIME_ENVELOPE_TEMPLATE_SHA256
    )
    target = json.loads(rendered)
    assert correction._changed_paths(json.loads(predecessor_raw), target) == {
        correction._MEMORY_PATH
    }


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

    assert correction.write_phase6_seller_command_runtime_envelope() == output_path
    assert output_path.read_bytes() == expected
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.parent.stat().st_mode) == 0o700
    assert correction.write_phase6_seller_command_runtime_envelope() == output_path

    output_path.write_bytes(b"different\n")
    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        correction.write_phase6_seller_command_runtime_envelope()


def test_private_write_rejects_symlinked_output_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path, _ = _bind_private_write(monkeypatch, tmp_path)
    repository = correction.REPOSITORY_ROOT
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (repository / ".mr_lister_private").symlink_to(outside, target_is_directory=True)

    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        correction.write_phase6_seller_command_runtime_envelope()
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

    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        correction.write_phase6_seller_command_runtime_envelope()
    assert not output_path.exists()


def test_private_write_rejects_nonprivate_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, _ = _bind_private_write(monkeypatch, tmp_path)
    public_output = correction.REPOSITORY_ROOT / "public/target.json"
    monkeypatch.setattr(correction, "DEFAULT_OUTPUT_PATH", public_output)

    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        correction.write_phase6_seller_command_runtime_envelope()
    assert not public_output.exists()


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
        with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
            correction.render_phase6_seller_command_runtime_envelope(
                predecessor_raw,
                source_raw,
            )


def test_render_rejects_duplicate_source(monkeypatch: pytest.MonkeyPatch) -> None:
    predecessor, _ = _documents()
    predecessor_raw = _canonical(predecessor)
    duplicate_source = b'{"Resources":{},"Resources":{}}'
    monkeypatch.setattr(
        correction,
        "PREDECESSOR_TEMPLATE_SHA256",
        sha256(predecessor_raw).hexdigest(),
    )
    monkeypatch.setattr(
        correction,
        "SOURCE_TEMPLATE_SHA256",
        sha256(duplicate_source).hexdigest(),
    )

    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        correction.render_phase6_seller_command_runtime_envelope(
            predecessor_raw,
            duplicate_source,
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
    monkeypatch.setattr(
        correction,
        "SELLER_COMMAND_RUNTIME_ENVELOPE_TEMPLATE_SHA256",
        "0" * 64,
    )

    with pytest.raises(correction.Phase6SellerCommandRuntimeEnvelopeError):
        correction.render_phase6_seller_command_runtime_envelope(predecessor_raw, source_raw)
