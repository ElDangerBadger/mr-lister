"""Current source advances independently from historical release reconstruction."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_evaluator_publication_status as evaluator
import tools.render_phase6_core_sam_staging as core
import tools.render_phase6_sam_activation as full
import tools.render_phase6_seller_command_runtime_envelope as seller

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "infra/phase6/template.json"
UPLOAD_MEMORY_PATH = ("Resources", "UploadApiFunction", "Properties", "MemorySize")
CLEAR_RECENT_PATH = (
    "Resources",
    "SellerCommandApiFunction",
    "Properties",
    "Events",
    "ClearRecentJobs",
)
HISTORICAL_SHA256 = "1f9772e04ace5a035febeea14b92417866326e5f07a0495a981648dac625fd09"
UPLOAD_MEMORY_SHA256 = "50d0d979726aa5f6232cc82a16f9e0a9df9d097168749595b65fe1995c60b3b7"
CURRENT_SHA256 = "85cc04b85015fd3c3b1bb4871aa19f0b56e6684a8860fd6c51d19a698f889623"
CLEAR_RECENT_EVENT = {
    "Type": "HttpApi",
    "Properties": {
        "ApiId": {"Ref": "SellerHttpApi"},
        "Path": "/v1/jobs/recent/clear",
        "Method": "POST",
        "PayloadFormatVersion": "2.0",
        "Auth": {
            "Authorizer": "SellerJwtAuthorizer",
            "AuthorizationScopes": ["mr-lister-api/seller"],
        },
    },
}


def _before_recent_clear_bytes(current_raw: bytes) -> bytes:
    event_block = (
        b'          "ClearRecentJobs": {\n'
        b'            "Type": "HttpApi",\n'
        b'            "Properties": {"ApiId": {"Ref": "SellerHttpApi"}, '
        b'"Path": "/v1/jobs/recent/clear", "Method": "POST", '
        b'"PayloadFormatVersion": "2.0", "Auth": {"Authorizer": "SellerJwtAuthorizer", '
        b'"AuthorizationScopes": ["mr-lister-api/seller"]}}\n'
        b"          },\n"
    )
    assert current_raw.count(event_block) == 1
    predecessor = current_raw.replace(event_block, b"", 1)
    assert sha256(predecessor).hexdigest() == UPLOAD_MEMORY_SHA256
    return predecessor


def _reviewed_predecessor_bytes(current_raw: bytes) -> bytes:
    # Verify the two exact source transitions against their sealed predecessor hashes
    # in memory. No historical scaffold is restored, rendered, or packaged for this check.
    current_raw = _before_recent_clear_bytes(current_raw)
    upload_memory = (
        b'"Handler": "phase6_lambda.upload_api_handler",\n'
        b'        "Role": {"Fn::GetAtt": ["UploadApiFunctionRole", "Arn"]},\n'
        b'        "MemorySize": 1024,'
    )
    assert current_raw.count(upload_memory) == 1
    return current_raw.replace(upload_memory, upload_memory.replace(b"1024", b"512"), 1)


def test_reviewed_source_transitions_preserve_all_other_authority() -> None:
    current_raw = CURRENT.read_bytes()
    historical_raw = _reviewed_predecessor_bytes(current_raw)
    assert seller.DEFAULT_SOURCE_PATH != CURRENT
    assert sha256(historical_raw).hexdigest() == HISTORICAL_SHA256 == seller.SOURCE_TEMPLATE_SHA256
    assert sha256(current_raw).hexdigest() == CURRENT_SHA256
    assert (
        evaluator.SOURCE_TEMPLATE_SHA256
        == core._SOURCE_TEMPLATE_SHA256
        == full._SOURCE_TEMPLATE_SHA256
        == CURRENT_SHA256
    )

    historical = json.loads(historical_raw)
    current = json.loads(current_raw)
    assert seller._changed_paths(historical, current) == {UPLOAD_MEMORY_PATH, CLEAR_RECENT_PATH}
    assert historical["Resources"]["UploadApiFunction"]["Properties"]["MemorySize"] == 512
    assert current["Resources"]["UploadApiFunction"]["Properties"]["MemorySize"] == 1024
    expected = deepcopy(historical)
    expected["Resources"]["UploadApiFunction"]["Properties"]["MemorySize"] = 1024
    expected["Resources"]["SellerCommandApiFunction"]["Properties"]["Events"]["ClearRecentJobs"] = (
        CLEAR_RECENT_EVENT
    )
    assert expected == current
    before_clear = json.loads(_before_recent_clear_bytes(current_raw))
    assert seller._changed_paths(before_clear, current) == {CLEAR_RECENT_PATH}

    # The current full staging accepts the upload envelope and clear route, but sealed core
    # reconstruction must keep its exact historical resource/provenance identity.
    assert full._load_scaffold_template(ROOT) == current
    assert core._load_source_template(ROOT) == current
    assert "UploadApiFunction" not in core._RESOURCE_TYPES
    assert "SellerCommandApiFunction" not in core._RESOURCE_TYPES
    for logical_id in core._RESOURCE_TYPES:
        assert current["Resources"][logical_id] == historical["Resources"][logical_id]
    assert core._SEALED_CORE_SOURCE_TEMPLATE_SHA256 == (
        "6b8221fd526cd06cf76cf0029d9c2cd6baf81662aeaa280aa573391e0dfdec3b"
    )


@pytest.mark.parametrize(
    "mutation", ["memory", "authority", "clear_auth", "whitespace", "predecessor", "before_clear"]
)
def test_current_renderers_reject_every_unreviewed_source_variant(
    tmp_path: Path, mutation: str
) -> None:
    source = tmp_path / "infra/phase6/template.json"
    source.parent.mkdir(parents=True)
    raw = CURRENT.read_bytes()
    document = json.loads(raw)
    if mutation == "memory":
        document["Resources"]["UploadApiFunction"]["Properties"]["MemorySize"] = 2048
        raw = json.dumps(document).encode()
    elif mutation == "authority":
        document["Globals"]["Function"]["Environment"]["Variables"][
            "MR_LISTER_PHASE6_SCAFFOLD_ONLY"
        ] = "false"
        raw = json.dumps(document).encode()
    elif mutation == "whitespace":
        raw += b"\n"
    elif mutation == "clear_auth":
        document["Resources"]["SellerCommandApiFunction"]["Properties"]["Events"][
            "ClearRecentJobs"
        ]["Properties"]["Auth"] = {"Authorizer": "NONE"}
        raw = json.dumps(document).encode()
    elif mutation == "before_clear":
        raw = _before_recent_clear_bytes(raw)
    else:
        raw = _reviewed_predecessor_bytes(raw)
    source.write_bytes(raw)
    with pytest.raises(ValueError):
        core._load_source_template(tmp_path)
    with pytest.raises(ValueError):
        full._load_scaffold_template(tmp_path)
    with pytest.raises(evaluator.EvaluatorPlanError, match="scaffold changed"):
        evaluator.render_evaluator_status_template({}, {}, repository=tmp_path)
