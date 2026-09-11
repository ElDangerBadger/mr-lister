"""Evaluator status remains authenticated, owner scoped, read only and default off."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_phase6_cloud_api import (
    CLIENT_ID,
    ISSUER,
    JOB_ID,
    OWNER,
    SCOPE,
    api_event,
    job_record,
)
from test_phase66_api_composition import exact_environment

from mr_lister.cloud.api import ReviewQueryApiAdapter
from mr_lister.cloud.auth import SellerClaimsPolicy
from mr_lister.cloud.evaluator_publication import (
    EVALUATOR_PUBLICATION_MESSAGE,
    EVALUATOR_PUBLICATION_ROUTE,
    EVALUATOR_PUBLICATION_SETTING,
    EvaluatorPublicationProjection,
    evaluator_publication_projection,
)
from mr_lister.cloud.phase6_composition import (
    Phase6ApiConfigurationError,
    build_query_api_handler,
    compose_query_api_adapter,
    load_query_api_configuration,
)
from mr_lister.control.errors import NotFoundError
from mr_lister.control.models import ControlJobState


class OwnedJobStore:
    def __init__(self, job=None):
        self.job = job
        self.calls = []

    def get_job_for_owner(self, owner_id, job_id):
        self.calls.append((owner_id, job_id))
        if self.job is None:
            raise NotFoundError
        return self.job


class NoOtherReads:
    def get(self, **_kwargs):
        pytest.fail("Evaluator status must not project review or contact a provider")

    def authorize(self, **_kwargs):
        pytest.fail("Evaluator status must not issue an artwork URL")


def adapter(store, *, enabled=True):
    return ReviewQueryApiAdapter(
        claims_policy=SellerClaimsPolicy(issuer=ISSUER, client_id=CLIENT_ID, required_scope=SCOPE),
        store=store,
        reviews=NoOtherReads(),
        previews=NoOtherReads(),
        evaluator_publication_status=enabled,
    )


def event(**kwargs):
    value = api_event(EVALUATOR_PUBLICATION_ROUTE, **kwargs)
    value["rawPath"] = f"/v1/jobs/{JOB_ID}/publication"
    value["requestContext"]["http"]["path"] = value["rawPath"]
    return value


def body(response):
    return json.loads(response["body"])


def test_owned_status_is_closed_disabled_etag_bound_and_never_exposes_internal_authority():
    store = OwnedJobStore(job_record())
    response = adapter(store)(event())
    assert response["statusCode"] == 200
    value = EvaluatorPublicationProjection.model_validate_json(response["body"])
    assert value.publication_enabled is False
    assert value.request_enabled is False
    assert value.request_disabled_reason == "EVALUATOR_PUBLICATION_DISABLED"
    assert value.request_disabled_message == EVALUATOR_PUBLICATION_MESSAGE
    assert value.state == "not_requested"
    assert response["headers"]["ETag"] == f'"{value.etag}"'
    assert "no-store" in response["headers"]["Cache-Control"]
    assert store.calls == [(OWNER, JOB_ID)]
    for private in (OWNER, "source_artifact_fingerprint", "printify", "access_token"):
        assert private not in response["body"]


def test_status_fingerprint_is_stable_and_changes_with_job_authority():
    original = job_record()
    first = evaluator_publication_projection(original, owner_id=OWNER, job_id=JOB_ID)
    assert first == evaluator_publication_projection(original, owner_id=OWNER, job_id=JOB_ID)
    changed = original.model_copy(update={"updated_at": original.updated_at + timedelta(seconds=1)})
    assert (
        first.etag != evaluator_publication_projection(changed, owner_id=OWNER, job_id=JOB_ID).etag
    )
    changed = original.model_copy(update={"record_version": original.record_version + 1})
    assert (
        first.etag != evaluator_publication_projection(changed, owner_id=OWNER, job_id=JOB_ID).etag
    )


def test_authentication_precedes_owned_job_read():
    store = OwnedJobStore(job_record())
    response = adapter(store)(event(authenticated=False))
    assert response["statusCode"] == 401
    assert not store.calls


@pytest.mark.parametrize(
    "claim,value",
    [("iss", "https://wrong.example"), ("scope", "openid"), ("cognito:groups", '["viewer"]')],
)
def test_seller_claims_are_required_before_owned_job_read(claim, value):
    store = OwnedJobStore(job_record())
    request = event()
    request["requestContext"]["authorizer"]["jwt"]["claims"][claim] = value
    request["requestContext"]["authorizer"]["jwt"]["scopes"] = []
    assert adapter(store)(request)["statusCode"] in {401, 403}
    assert not store.calls


@pytest.mark.parametrize(
    "job", [None, job_record(owner_id="f" * 64), job_record(job_id="foreign_job")]
)
def test_missing_and_foreign_jobs_return_the_same_not_found(job):
    response = adapter(OwnedJobStore(job))(event())
    assert response["statusCode"] == 404
    assert body(response)["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "mutation",
    ["query", "body", "path", "http_method", "http_path", "extra_id", "version", "encoded"],
)
def test_invalid_get_envelopes_cannot_read_the_store(mutation):
    request = event()
    if mutation == "query":
        request["rawQueryString"] = "owner_id=someone"
        request["queryStringParameters"] = {"owner_id": "someone"}
    elif mutation == "body":
        request["body"] = "{}"
    elif mutation == "path":
        request["rawPath"] += "/"
    elif mutation == "http_method":
        request["requestContext"]["http"]["method"] = "POST"
    elif mutation == "http_path":
        request["requestContext"]["http"]["path"] = "/other"
    elif mutation == "extra_id":
        request["pathParameters"]["owner_id"] = OWNER
    elif mutation == "version":
        request["version"] = "1.0"
    else:
        request["isBase64Encoded"] = True
    store = OwnedJobStore(job_record())
    assert adapter(store)(request)["statusCode"] == 400
    assert not store.calls


def test_existing_publication_authority_is_never_masked_as_not_requested():
    job = job_record().model_copy(
        update={
            "state": ControlJobState.APPROVED,
            "active_work_request_id": None,
            "publication_aggregate_id": "publication_existing",
        }
    )
    response = adapter(OwnedJobStore(job))(event())
    assert response["statusCode"] == 503
    assert body(response)["error"]["code"] == "PROJECTION_UNAVAILABLE"
    assert "publication_existing" not in response["body"]


@pytest.mark.parametrize(
    "route", ["POST /v1/jobs/{job_id}/publish", "POST /v1/jobs/{job_id}/publication", "$default"]
)
def test_no_evaluator_publish_or_fallback_route_exists(route):
    store = OwnedJobStore(job_record())
    request = event()
    request["routeKey"] = route
    assert adapter(store)(request)["statusCode"] == 404
    assert not store.calls


def test_production_default_keeps_evaluator_get_absent():
    store = OwnedJobStore(job_record())
    assert adapter(store, enabled=False)(event())["statusCode"] == 404
    configuration = load_query_api_configuration(exact_environment())
    assert configuration.evaluator_publication_status is False
    handler = build_query_api_handler(
        exact_environment(), client_factory=lambda *_a, **_k: pytest.fail("No client needed")
    )
    assert handler(event())["statusCode"] == 404
    assert not store.calls


def test_server_policy_wires_the_lazy_route_and_query_adapter(monkeypatch):
    from test_phase66_api_composition import RecordingClientFactory

    import mr_lister.cloud.phase6_composition as composition

    environment = {**exact_environment(), EVALUATOR_PUBLICATION_SETTING: "read_only"}
    configuration = load_query_api_configuration(environment)
    assert configuration.evaluator_publication_status is True
    composed = compose_query_api_adapter(configuration, client_factory=RecordingClientFactory())
    assert EVALUATOR_PUBLICATION_ROUTE in composed._allowed_routes
    store = OwnedJobStore(job_record())
    monkeypatch.setattr(composition, "compose_query_api_adapter", lambda *_a, **_k: adapter(store))
    assert build_query_api_handler(environment)(event())["statusCode"] == 200
    assert len(store.calls) == 1


@pytest.mark.parametrize("value", [True, False, None, 1, "true", "enabled", "READ_ONLY", ""])
def test_malformed_server_policy_is_rejected_without_fallback(value):
    with pytest.raises(Phase6ApiConfigurationError):
        load_query_api_configuration({**exact_environment(), EVALUATOR_PUBLICATION_SETTING: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", "7.1.0"),
        ("publication_enabled", True),
        ("publication_enabled", 0),
        ("request_enabled", 0),
        ("request_enabled", True),
        ("state", "published"),
        ("safe_listing_url", "https://www.etsy.com/listing/123"),
        ("request_disabled_reason", "PUBLICATION_NOT_ELIGIBLE"),
        ("request_disabled_message", "Publish now"),
        ("aggregate_record_version", 1),
        ("notification_available", True),
        ("unexpected", "authority"),
    ],
)
def test_disabled_projection_cannot_be_promoted_to_publication_authority(field, value):
    exact = evaluator_publication_projection(job_record(), owner_id=OWNER, job_id=JOB_ID)
    with pytest.raises(ValidationError):
        EvaluatorPublicationProjection.model_validate({**exact.model_dump(), field: value})
