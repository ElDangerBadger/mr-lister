"""Tag revisions do not redefine the restored writing/review prompts.

These offline checks establish prompt isolation and request boundaries, not model
tag quality or search performance; those need separately reviewed artwork trials.
"""

from __future__ import annotations

import pytest
from test_harness_candidate import (
    ScriptedClient,
    draft,
    evidence,
    images,
    png_bytes,
    prepare,
    request_text,
    response,
)

from mr_lister.intelligence.harness_candidate import (
    WRITER_IMAGE_VERIFICATION_PROMPT_V3,
    candidate_prompt_bundles,
)

V4_PROMPTS = {
    "evidence": (
        "2026-10-04.3-evidence-brief",
        "2cf84c8b8fde1add5affa0eb51ac67f72e1acc3658df5dc44c02773cfff5cba8",
    ),
    "writer": (
        "2026-10-04.4-original-writer",
        "dcdd2e248371f5dbf9380d0e869332657f4270155f8c2d230c74aa8759a71f68",
    ),
    "full": (
        "2026-10-04.5-original-writer-harness",
        "fef64b014379eceae1d8bd421010acef01105be4e85a2c75b584beb3a5ac536e",
    ),
}
V5_PROMPTS = {
    "evidence": V4_PROMPTS["evidence"],
    "writer": (
        "2026-10-05.1-tag-coverage-writer",
        "e6d284190c05aae82cb4c13c4fadb67d6d872e8bf092742256667765f5b64333",
    ),
    "full": (
        "2026-10-05.2-tag-coverage-harness",
        "0d2d13d552677e008c9f2a61b5f5ad113321274941fed4ef8befd6060819ee22",
    ),
}
V6_PROMPTS = {
    "evidence": V4_PROMPTS["evidence"],
    "writer": (
        "2026-10-05.3-tag-length-writer",
        "8aff34e7be99641c86945e33cb21d0a91b135e47d4fa65f43cb21a113ef74153",
    ),
    "full": (
        "2026-10-05.4-tag-length-harness",
        "5e0e88750e43f8acbda97ff23cf9876dad34c47c1a37bc3d50cfcba062139b56",
    ),
}


def tag_section(prompt):
    before, start, remainder = prompt.partition("TAG CANDIDATES\n")
    tags, end, after = remainder.partition("AUDIENCE AND RATIONALES\n")
    assert start and end
    return before, tags, after


@pytest.mark.parametrize(
    "revision, expected", [("v4", V4_PROMPTS), ("v5", V5_PROMPTS), ("v6", V6_PROMPTS)]
)
def test_tag_revision_has_frozen_identity_without_relabeling_restored_writer(revision, expected):
    bundles = candidate_prompt_bundles(revision=revision)

    assert {
        key: (bundle.version, bundle.fingerprint) for key, bundle in bundles.items()
    } == expected
    assert candidate_prompt_bundles() == candidate_prompt_bundles(revision="v1")


@pytest.mark.parametrize("arm", ["writer", "full"])
@pytest.mark.parametrize("revision", ["v5", "v6"])
def test_tag_revisions_preserve_every_other_prompt_byte(arm, revision):
    restored = candidate_prompt_bundles(revision="v4")
    revised = candidate_prompt_bundles(revision=revision)
    before, old_tags, after = tag_section(restored[arm].listing)
    revised_before, new_tags, revised_after = tag_section(revised[arm].listing)

    # Before contains the preamble, design-hook analysis, title and description;
    # after contains audience/rationales, the input and any image verification.
    assert revised_before == before
    assert revised_after == after
    assert new_tags != old_tags
    assert revised[arm].system == restored[arm].system
    assert revised[arm].repair == restored[arm].repair
    assert revised[arm].artwork == restored[arm].artwork
    assert revised["evidence"] == restored["evidence"]
    assert revised[arm].listing.count("{analysis_json}") == 1
    assert revised["full"].listing.endswith(WRITER_IMAGE_VERIFICATION_PROMPT_V3)


@pytest.mark.parametrize("revision", ["v5", "v6"])
def test_writer_only_and_image_writer_use_identical_tag_guidance(revision):
    revised = candidate_prompt_bundles(revision=revision)

    assert tag_section(revised["writer"].listing)[1] == tag_section(revised["full"].listing)[1]


@pytest.mark.parametrize("revision", ["v5", "v6"])
def test_tag_revisions_preserve_the_actual_two_call_boundary(revision):
    content = png_bytes()
    calls = {}
    for selected in ("v4", revision):
        client = ScriptedClient(response(evidence()), response(draft()))
        result = prepare(client, content, revision=selected)
        assert result.state == "accepted_for_evaluation"
        assert len(client.calls) == 2
        assert (
            result.prompt_fingerprint
            == candidate_prompt_bundles(revision=selected)["full"].fingerprint
        )
        calls[selected] = client.calls

    assert calls["v4"][0] == calls[revision][0]
    old, new = calls["v4"][1], calls[revision][1]
    assert images(old) == images(new)
    assert {key: value for key, value in old.items() if key != "messages"} == {
        key: value for key, value in new.items() if key != "messages"
    }
    before, old_tags, after = tag_section(request_text(old))
    new_before, new_tags, new_after = tag_section(request_text(new))
    assert new_before == before
    assert new_after == after
    assert new_tags != old_tags


def test_v6_length_headroom_keeps_v5_subject_and_semantic_coverage_guidance():
    old = tag_section(candidate_prompt_bundles(revision="v5")["full"].listing)[1]
    new = tag_section(candidate_prompt_bundles(revision="v6")["full"].listing)[1]
    unchanged_start = "- Prioritize the actual subject"

    assert new.split(unchanged_start, 1)[1] == old.split(unchanged_start, 1)[1]
    assert "around 12 to 16 characters" in new
    assert "even when it is longer than 16 characters" in new
    assert "hard maximum is 20 characters" in new
    assert "space and punctuation mark" in new
    assert "Never truncate a phrase" in new
