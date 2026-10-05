"""Ordinary native accounts cross the real onboarding/draft/publication boundaries offline.

AWS, model observations, and provider responses are deterministic test boundaries. These tests
exercise the application services and adapters together; they neither provision AWS nor publish
a real product. Dynamo epoch-claim race tests remain in test_store_binding_pipeline.py.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from mr_lister.accounts.models import (
    AccountConfig,
    owner_id_for,
)
from mr_lister.accounts.provision import (
    AccountProvisioner,
    CognitoAccountGroupWriter,
    native_signup,
)
from mr_lister.accounts.store import DynamoAccountWriter
from mr_lister.cloud.api import UploadApiAdapter
from mr_lister.cloud.auth import SellerClaimsPolicy
from mr_lister.cloud.phase7_provider_credentials import (
    ProductionPublicationProviderCredentialAuthority,
)
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.connections.credentials import ExactConnectionResolver, SecretsManagerCredentialStore
from mr_lister.connections.http import ConnectionHttpHandler
from mr_lister.connections.models import (
    Connection,
    ConnectionConfig,
    ConnectionConflict,
    ConnectionError,
    Setup,
)
from mr_lister.connections.service import ConnectionService, SellerGroupWriter
from mr_lister.connections.store import (
    DynamoConnectionDirectory,
    DynamoConnectionStore,
    connection_key,
    setup_key,
)
from mr_lister.connections.transport import PrintifyShopsTransport
from mr_lister.control.commands import ApproveReviewCommand
from mr_lister.control.fingerprints import canonical_fingerprint, review_etag
from mr_lister.control.models import ControlJobState, ProductMockupEvidence, ProductVariantEvidence
from mr_lister.control.service import SellerControlService
from mr_lister.control.store import InMemorySellerControlStore
from mr_lister.control.upload_service import UploadIntakeService
from mr_lister.control.worker_commands import (
    BeginPreparationCommand,
    BeginProviderUploadCommand,
    BeginProviderWriteCommand,
    CompletePreparationWithAgentDecisionCommand,
    ProductSyncObservation,
    RecordPreparedReviewCommand,
    RecordPricingSuccessCommand,
    RecordProductSyncSuccessCommand,
    RecordProviderUploadSuccessCommand,
    UploadedArtworkObservation,
)
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.production.draft_sync import build_canonical_draft
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyResolvedProfile,
    PrintifyResolvedVariant,
)
from mr_lister.production.provider_resources import OwnerBoundProviderDraftResources
from mr_lister.publication.contract import PublicationState
from mr_lister.publication.execution_commands import (
    ClaimShopGetCommand,
    DispatchPublicationWorkCommand,
    ReconstructPublicationAuthorityCommand,
    RecordPublicationPostOutcomeCommand,
    RecordPublicationPreflightCommand,
    RecordPublicationProductObservationCommand,
)
from mr_lister.publication.execution_models import PublicationCallPurpose
from mr_lister.publication.execution_service import PublicationExecutionService
from mr_lister.publication.execution_store import InMemoryPublicationExecutionStore
from mr_lister.publication.provider_boundary import (
    PrintifyPublicationBoundary,
    PublicationHttpResponse,
)
from mr_lister.publication.provider_credentials import PublicationProviderCredentialError
from mr_lister.publication.service import PublicationRequestService
from mr_lister.publication.store import InMemoryPublicationStore, PublicationRequestAuthority
from mr_lister.review_profile import ExactReviewProductProfile
from tests.test_account_backend import DynamoFake as AccountDynamoFake
from tests.test_account_backend import confirmation
from tests.test_connection_backend import TOKEN, SecretsFake
from tests.test_connection_backend import DynamoFake as ConnectionDynamoFake
from tests.test_phase6_upload_service import FakeUploadArtifacts, MutableClock, _png
from tests.test_phase6_worker_service import (
    _activate,
    _agent_decision,
    _analysis,
    _estimate_for_current_sync,
    _listing,
)
from tests.test_phase71_publication_service import (
    ProfileAuthority,
    _profile,
    profile_eligibility_authority,
)
from tests.test_phase71_publication_service import (
    _command as publish_command,
)
from tests.test_phase72_publication_execution import Harness as PublicationHarness
from tests.test_phase72_publication_provider_boundary import MemoryAudit, ScriptedTransport

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SUBJECT = "11111111-2222-4333-8444-555555555555"
SECOND_SUBJECT = "22222222-3333-4444-8555-666666666666"
PRIMARY_SUBJECT = "33333333-4444-4555-8666-777777777777"
JUDGE_SUBJECT = "44444444-5555-4666-8777-888888888888"
RELEASE = "f" * 64


class CognitoBoundary:
    def __init__(self):
        self.calls = []
        self.memberships = set()
        self.deny_seller = False

    def admin_add_user_to_group(self, **request):
        self.calls.append(copy.deepcopy(request))
        assert request["GroupName"] in {"account", "seller"}
        if request["GroupName"] == "seller" and self.deny_seller:
            raise RuntimeError("group temporarily unavailable")
        self.memberships.add((request["Username"], request["GroupName"]))
        return {}


class ShopsBoundary:
    """Fake wire adapter around the real fixed GET /v1/shops.json transport."""

    def __init__(self):
        self.requests = []
        self.rows = [
            {"id": 7, "name": "Merchant Etsy Store", "sales_channel": "etsy"},
            {"id": 8, "name": "Merchant Other Channel", "sales_channel": "shopify"},
        ]

    def connect(self, host, **kwargs):
        assert host == "api.printify.com" and kwargs["timeout"] == 5
        outer = self

        class Session:
            def request(self, method, path, headers):
                assert method == "GET" and path == "/v1/shops.json"
                assert headers["Authorization"] == "Bearer " + TOKEN
                outer.requests.append((method, path, copy.deepcopy(headers)))

            def getresponse(self):
                body = json.dumps(outer.rows).encode()
                return SimpleNamespace(
                    status=200,
                    getheader=lambda key, default="": "application/json",
                    read=lambda size: body[:size],
                )

            def close(self):
                pass

        return Session()


class ExactOnlyResolver:
    def __init__(self, resolver):
        self.resolver = resolver
        self.exact_calls = []
        self.legacy_calls = []

    def resolve_exact(self, *, binding):
        self.exact_calls.append(binding)
        return self.resolver.resolve_exact(binding=binding)

    def resolve(self, *, owner_id):
        self.legacy_calls.append(owner_id)
        raise AssertionError("An ordinary merchant must not request an owner/judge credential")


class CapturePublicationStore(InMemoryPublicationStore):
    def __init__(self, authorities):
        super().__init__(authorities)
        self.transaction = None

    def commit_request(self, transaction):
        result = super().commit_request(transaction)
        self.transaction = transaction
        return result


class ExecutionClock:
    def __init__(self, clock):
        self._clock = clock

    @property
    def now(self):
        return self._clock.value

    def __call__(self):
        return self._clock.value

    def tick(self, seconds=1):
        self._clock.advance(timedelta(seconds=seconds))


class MerchantPublicationHarness(PublicationHarness):
    """Reuse the existing real execution/ledger helper with the native merchant owner."""

    def __init__(self, transaction, workflow):
        self.transaction = transaction
        self.owner_id = workflow.account.owner_id
        self.aggregate_id = transaction.commit.aggregate.aggregate_id
        self.clock = ExecutionClock(workflow.clock)
        self.store = InMemoryPublicationExecutionStore((transaction,))
        self.profile_eligibility = profile_eligibility_authority(workflow.exact_profile)
        self.service = PublicationExecutionService(
            self.store,
            profiles=workflow.profiles,
            profile_eligibility=self.profile_eligibility,
            release_manifest_fingerprint=RELEASE,
            clock=self.clock,
        )
        self.operation_number = 0

    @property
    def authority(self):
        return self.store.load_execution_authority(self.owner_id, self.aggregate_id)

    def command(self, command_type, name, **values):
        command = super().command(command_type, name, **values)
        return command_type.model_validate(
            {**command.model_dump(mode="python"), "owner_id": self.owner_id}
        )


class Workflow:
    def __init__(self, *, enabled=True, fail_grant=False):
        self.clock = MutableClock(NOW)
        self.account_dynamo = AccountDynamoFake()
        self.connection_dynamo = ConnectionDynamoFake()
        self.cognito = CognitoBoundary()
        self.cognito.deny_seller = fail_grant
        base = AccountConfig(
            "us-west-2_IntegrationPool", "integrationclient", "Accounts", frozenset({"f" * 64})
        )
        self.account_config = replace(
            base,
            reserved_owner_ids=frozenset(
                owner_id_for(base.issuer, subject) for subject in (PRIMARY_SUBJECT, JUDGE_SUBJECT)
            ),
        )
        self.connection_config = ConnectionConfig(
            self.account_config, "Connections", "mr-lister/integration/connections/", enabled
        )
        self.accounts = DynamoAccountWriter(self.account_dynamo, self.account_config)
        self.provision = AccountProvisioner(
            self.account_config,
            store=self.accounts,
            groups=CognitoAccountGroupWriter(self.cognito, self.account_config),
            clock=self.epoch,
        )
        self.account = self.signup(SUBJECT)
        self.secrets = SecretsFake()
        self.credentials = SecretsManagerCredentialStore(self.secrets, self.connection_config)
        self.directory = DynamoConnectionStore(self.connection_dynamo, self.connection_config)
        self.shops = ShopsBoundary()
        self.setup = ConnectionService(
            config=self.connection_config,
            store=self.directory,
            credentials=self.credentials,
            provider=PrintifyShopsTransport(connection_factory=self.shops.connect),
            groups=SellerGroupWriter(self.cognito, self.connection_config),
            clock=self.epoch,
        )
        self.resolver = ExactOnlyResolver(
            ExactConnectionResolver(directory=self.directory, credentials=self.credentials)
        )
        self.profile = _profile()
        self.exact_profile = ExactReviewProductProfile(
            profile=self.profile, fingerprint=canonical_fingerprint(self.profile)
        )
        self.profiles = ProfileAuthority(self.exact_profile)
        self.controls = InMemorySellerControlStore(binding_guard=self.directory)
        self.artifacts = FakeUploadArtifacts()
        self.uploads = UploadIntakeService(
            store=self.controls,
            artifacts=self.artifacts,
            profiles=self.profiles,
            artifact_bucket="integration-private",
            profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            clock=self.clock,
        )
        self.worker = WorkerControlService(store=self.controls, clock=self.clock)
        self.seller = SellerControlService(store=self.controls, clock=self.clock)

    def epoch(self):
        return int(self.clock.value.timestamp())

    def signup(self, subject):
        event = confirmation(self.account_config, subject)
        signup = native_signup(event, self.account_config)
        assert signup is not None
        return self.provision.provision(signup)

    def connect(self, account=None, *, key="new-merchant"):
        account = account or self.account
        candidate = self.setup.validate(account, token=TOKEN, idempotency_key=key + "-validate")
        ready = self.setup.select(
            account,
            candidate_id=candidate["candidate_id"],
            shop_id=7,
            expected_setup_version=candidate["record_version"],
            idempotency_key=key + "-select",
        )
        return candidate, ready

    def binding(self, ready, account=None):
        account = account or self.account
        return self.directory.get_active_binding(
            account.owner_id, ready["store"]["shop_binding_id"], ready["record_version"]
        )

    def approve_draft(self, pinned):
        content = _png()
        created = self.uploads.create_upload(
            owner_id=self.account.owner_id,
            idempotency_key="ordinary-artwork",
            filename="merchant-art.png",
            content_type="image/png",
            content_sha256=sha256(content).hexdigest(),
            size_bytes=len(content),
            store_binding=pinned,
        )
        self.artifacts.stage(content)
        self.uploads.complete_upload(
            owner_id=self.account.owner_id,
            upload_id=created.receipt.upload_id,
            idempotency_key="finish-artwork",
        )
        job_id = created.receipt.job_id
        job = self.controls.get_job(job_id)
        work = _activate(self.controls, job, clock=self.clock)
        started = self.worker.begin_preparation(
            BeginPreparationCommand(
                job_id=job_id,
                work_request_id=work.work_request_id,
                expected_record_version=job.record_version,
            )
        )
        source = self.controls.get_source_artifact(job_id)
        prepared = self.worker.record_prepared_review(
            RecordPreparedReviewCommand(
                job_id=job_id,
                work_request_id=work.work_request_id,
                expected_record_version=started.record_version,
                source_artifact_fingerprint=source.fingerprint,
                artwork_analysis=_analysis(),
                listing=_listing(),
                product_profile_fingerprint=self.exact_profile.fingerprint,
            )
        )
        self.worker.complete_preparation_with_agent_decision(
            CompletePreparationWithAgentDecisionCommand(
                job_id=job_id,
                work_request_id=work.work_request_id,
                expected_record_version=prepared.record_version,
                correlation_id="2" * 24,
                controller_model_id="google.gemma-4",
                tool_calls=("record_prepared_review",),
                cycles=2,
                input_tokens=800,
                output_tokens=200,
                total_tokens=1000,
                decision=_agent_decision(),
            )
        )
        job = self.controls.get_job(job_id)
        sync_work = _activate(self.controls, job, clock=self.clock)
        file_name = self.worker.upload_file_name(job_id, source.content_sha256)
        self.worker.begin_provider_upload(
            BeginProviderUploadCommand(
                job_id=job_id,
                work_request_id=sync_work.work_request_id,
                expected_record_version=job.record_version,
                source_artifact_fingerprint=source.fingerprint,
                file_name=file_name,
            )
        )
        uploading = self.controls.get_job(job_id)
        assert self.worker.authorize_provider_upload(
            job_id=job_id, attempt_id=uploading.provider_upload_attempt_id
        )
        self.worker.record_provider_upload_success(
            RecordProviderUploadSuccessCommand(
                job_id=job_id,
                work_request_id=sync_work.work_request_id,
                expected_record_version=uploading.record_version,
                attempt_id=uploading.provider_upload_attempt_id,
                observation=UploadedArtworkObservation(
                    image_id="merchant_image",
                    file_name=file_name,
                    width=2,
                    height=2,
                    size_bytes=source.size_bytes,
                ),
            )
        )
        uploaded = self.controls.get_job(job_id)
        self.draft = build_canonical_draft(
            job_id=job_id,
            listing=_listing(),
            profile=self.profile,
            image_id="merchant_image",
            artwork_width=2,
            artwork_height=2,
            resolved=PrintifyResolvedProfile(
                profile_id=self.profile.profile_id,
                profile_version=self.profile.profile_version,
                shop_id=pinned.shop_id,
                blueprint_id=self.profile.blueprint_id,
                print_provider_id=self.profile.print_provider_id,
                variants=(
                    PrintifyResolvedVariant(
                        variant_id=101,
                        color="Black",
                        size="S",
                        placement_group_id="small",
                        canvas_width=3021,
                        canvas_height=3927,
                        retail_price_cents=2999,
                    ),
                ),
            ),
        )
        self.worker.begin_provider_write(
            BeginProviderWriteCommand(
                job_id=job_id,
                work_request_id=sync_work.work_request_id,
                expected_record_version=uploaded.record_version,
                image_id="merchant_image",
                target_payload_fingerprint=self.draft.payload_fingerprint,
                correlation_token="ml-"
                + sha256(f"mr-lister:provider-draft:{job_id}".encode()).hexdigest()[:24],
            )
        )
        writing = self.controls.get_job(job_id)
        assert self.worker.authorize_provider_call(
            job_id=job_id, attempt_id=writing.provider_write_attempt_id
        )
        self.worker.record_product_sync_success(
            RecordProductSyncSuccessCommand(
                job_id=job_id,
                work_request_id=sync_work.work_request_id,
                expected_record_version=writing.record_version,
                attempt_id=writing.provider_write_attempt_id,
                observation=ProductSyncObservation(
                    product_id="merchant_product",
                    image_id="merchant_image",
                    printify_shop_id=pinned.shop_id,
                    request_fingerprint=self.draft.payload_fingerprint,
                    response_fingerprint="e" * 64,
                    mockups=(
                        ProductMockupEvidence(
                            url="https://images.printify.com/mockup/merchant/101/front.png",
                            position="front",
                            variant_ids=(101,),
                        ),
                    ),
                    variants=(
                        ProductVariantEvidence(
                            variant_id=101,
                            color="Black",
                            size="S",
                            placement_group_id="small",
                            retail_price_cents=2999,
                            production_cost_cents=1125,
                        ),
                    ),
                ),
            )
        )
        pricing_job = self.controls.get_job(job_id)
        pricing_work = _activate(self.controls, pricing_job, clock=self.clock)
        estimate = _estimate_for_current_sync(
            self.controls, pricing_job, calculated_at=self.clock.value
        )
        self.worker.record_pricing_success(
            RecordPricingSuccessCommand(
                job_id=job_id,
                work_request_id=pricing_work.work_request_id,
                expected_record_version=pricing_job.record_version,
                estimate=estimate,
            )
        )
        reviewable = self.controls.get_job(job_id)
        review = self.controls.get_review(job_id, reviewable.review_version)
        sync = self.controls.get_product_sync(job_id, reviewable.product_sync_id)
        pricing = self.controls.get_pricing(job_id, reviewable.pricing_snapshot_id)
        etag = review_etag(
            job_id=job_id,
            review_version=review.review_version,
            review_fingerprint=review.fingerprint,
            product_id=sync.product_id,
            product_sync_fingerprint=sync.fingerprint,
            pricing_snapshot_id=pricing.snapshot_id,
            pricing_snapshot_fingerprint=pricing.fingerprint,
        )
        self.seller.approve_review(
            ApproveReviewCommand(
                job_id=job_id,
                owner_id=self.account.owner_id,
                expected_record_version=reviewable.record_version,
                expected_review_version=review.review_version,
                expected_review_fingerprint=review.fingerprint,
                expected_review_etag=etag,
                idempotency_key="approve-ordinary-draft",
            )
        )
        approved = self.controls.get_job(job_id)
        return PublicationRequestAuthority(
            current_job=approved,
            review=review,
            product_sync=sync,
            approval_decision=self.controls.get_review_decision(
                job_id, approved.approval_decision_id
            ),
            source=source,
            pricing_snapshot=pricing,
            pricing_evidence=self.controls.get_pricing_evidence(job_id, pricing.snapshot_id),
        )

    def provider_product(self, shop_id, *, published=False):
        payload = self.draft.provider_create_payload()
        return {
            "id": "merchant_product",
            "shop_id": shop_id,
            **payload,
            "variants": [
                {**variant, "cost": 1125, "title": "Black / S"} for variant in payload["variants"]
            ],
            "images": [
                {
                    "src": "https://images.printify.com/mockup/merchant/101/front.png",
                    "position": "front",
                    "variant_ids": [101],
                    "is_default": True,
                }
            ],
            "is_locked": False,
            "visible": published,
            "external": {"id": "123456789"} if published else {},
        }

    def request_transaction(self, authority):
        request_store = CapturePublicationStore((authority,))
        eligibility = profile_eligibility_authority(self.exact_profile)
        request = PublicationRequestService(
            store=request_store,
            profiles=self.profiles,
            profile_eligibility=eligibility,
            release_manifest_fingerprint=RELEASE,
            clock=self.clock,
        )
        response = request.request_publication(publish_command(authority))
        transaction = request_store.transaction
        assert transaction is not None
        return response, transaction, eligibility

    def request_and_reconstruct(self, authority):
        response, transaction, eligibility = self.request_transaction(authority)
        execution_store = InMemoryPublicationExecutionStore((transaction,))
        service = PublicationExecutionService(
            execution_store,
            profiles=self.profiles,
            profile_eligibility=eligibility,
            release_manifest_fingerprint=RELEASE,
            clock=self.clock,
        )

        def current():
            return execution_store.load_execution_authority(
                self.account.owner_id, response.publication_aggregate_id
            )

        def command(kind, operation):
            value = current()
            return kind(
                owner_id=value.snapshot.owner_id,
                aggregate_id=value.aggregate.aggregate_id,
                operation_id=operation,
                expected_aggregate_record_version=value.aggregate.record_version,
                expected_aggregate_fingerprint=value.aggregate.fingerprint,
                expected_provider_evidence_record_version=value.aggregate.provider_evidence_record_version,
                expected_attempt_record_version=value.attempt.record_version,
                expected_permit_record_version=value.permit.record_version,
                expected_work_record_version=value.work.record_version,
            )

        service.dispatch_work(command(DispatchPublicationWorkCommand, "dispatch-ordinary"))
        self.clock.advance(timedelta(seconds=1))
        service.reconstruct_authority(
            command(ReconstructPublicationAuthorityCommand, "reconstruct-ordinary")
        )
        result = service.claim_shop_get(command(ClaimShopGetCommand, "shop-ordinary"))
        return current(), result


def event(config, subject=SUBJECT, *, groups=("account",), route="POST /v1/uploads", body=None):
    method, path = route.split(" ", 1)
    return {
        "version": "2.0",
        "routeKey": route,
        "rawPath": path,
        "rawQueryString": "",
        "isBase64Encoded": False,
        "headers": {"idempotency-key": "http-ordinary-user"},
        "body": body,
        "requestContext": {
            "requestId": "integration-request",
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "iss": config.issuer,
                        "client_id": config.client_id,
                        "sub": subject,
                        "token_use": "access",
                        "scope": "openid mr-lister-api/seller",
                        "cognito:groups": list(groups),
                    }
                }
            },
        },
    }


def test_native_account_owns_connection_upload_approval_and_exact_publication_graph():
    system = Workflow()
    assert system.cognito.memberships == {(SUBJECT, "account")}
    candidate, ready = system.connect()
    pinned = system.binding(ready)
    assert system.cognito.memberships == {(SUBJECT, "account"), (SUBJECT, "seller")}
    assert pinned.owner_id == system.account.owner_id and pinned.shop_id == 7
    assert candidate["stores"][1]["eligible"] is False
    authority = system.approve_draft(pinned)
    assert authority.current_job.state is ControlJobState.APPROVED
    assert authority.source.store_binding == authority.product_sync.store_binding == pinned
    execution, claim_result = system.request_and_reconstruct(authority)
    provider = execution.provider_authority
    assert (
        provider is not None
        and provider.store_binding == execution.snapshot.store_binding == pinned
    )
    credentials = ProductionPublicationProviderCredentialAuthority(connections=system.resolver)
    lower = credentials.resolve_exact(authority=provider).for_authority(provider)
    transport = ScriptedTransport(
        [
            PublicationHttpResponse(
                status=200,
                body=json.dumps(
                    [{"id": 7, "title": "Merchant Etsy Store", "sales_channel": "etsy"}]
                ).encode(),
            )
        ]
    )
    boundary = PrintifyPublicationBoundary(
        authority=provider,
        credential=lower,
        transport=transport,
        audit_sink=MemoryAudit(),
        clock=system.clock,
    )
    observation = boundary.preflight_shop(
        call_claim=execution.call_claims[-1], fresh_grant=claim_result.fresh_call_grant
    )
    assert observation.printify_shop_id == pinned.shop_id
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert len(transport.calls) == 1 and transport.calls[0]["method"] == "GET"
    assert not system.resolver.legacy_calls
    assert all(
        name.startswith(system.connection_config.secret_prefix + system.account.owner_id + "/")
        for name in system.secrets.items
    )
    assert TOKEN not in json.dumps(system.account_dynamo.items)
    assert TOKEN not in json.dumps(system.connection_dynamo.items)
    assert TOKEN not in json.dumps(ready)


def test_ordinary_store_one_shot_publish_targets_its_shop_and_verifies_its_etsy_listing_offline():
    system = Workflow()
    _, ready = system.connect()
    pinned = system.binding(ready)
    approved = system.approve_draft(pinned)
    _, transaction, _ = system.request_transaction(approved)
    harness = MerchantPublicationHarness(transaction, system)
    harness.dispatch_and_reconstruct()
    provider = harness.authority.provider_authority
    assert provider is not None and provider.store_binding == pinned
    credentials = ProductionPublicationProviderCredentialAuthority(connections=system.resolver)
    lower = credentials.resolve_exact(authority=provider).for_authority(provider)
    responses = [
        [{"id": 7, "title": "Merchant Etsy Store", "sales_channel": "etsy"}],
        system.provider_product(pinned.shop_id),
        {},
        system.provider_product(pinned.shop_id, published=True),
    ]
    transport = ScriptedTransport(
        [
            PublicationHttpResponse(status=200, body=json.dumps(payload).encode())
            for payload in responses
        ]
    )
    boundary = PrintifyPublicationBoundary(
        authority=provider,
        credential=lower,
        transport=transport,
        audit_sink=MemoryAudit(),
        clock=harness.clock,
    )
    shop_result, shop_claim = harness.claim_shop()
    shop_evidence = boundary.preflight_shop(
        call_claim=shop_claim, fresh_grant=shop_result.fresh_call_grant
    )
    product_result, product_claim = harness.claim_product(PublicationCallPurpose.PRODUCT_PREFLIGHT)
    product_evidence = boundary.preflight_exact_product(
        call_claim=product_claim, fresh_grant=product_result.fresh_call_grant
    )
    harness.service.record_preflight(
        harness.command(
            RecordPublicationPreflightCommand,
            "merchant-preflight",
            shop_evidence=shop_evidence,
            product_evidence=product_evidence,
        )
    )
    publish_result, publish_claim = harness.claim_publish()
    current = harness.authority
    published = boundary.publish_exact_product(
        call_claim=publish_claim,
        mutation_claim=current.mutation_claim,
        preflight_proof=current.preflight_proof,
        fresh_grant=publish_result.fresh_call_grant,
    )
    harness.service.record_post_outcome(
        harness.command(RecordPublicationPostOutcomeCommand, "merchant-post", evidence=published)
    )
    harness.clock.tick()
    verify_result, verify_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    verified = boundary.poll_exact_product(
        call_claim=verify_claim, fresh_grant=verify_result.fresh_call_grant
    )
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "merchant-etsy-verification",
            evidence=verified,
        )
    )
    final = harness.authority
    assert final.aggregate.state is PublicationState.PUBLISHED
    assert final.result.safe_listing_url == "https://www.etsy.com/listing/123456789"
    assert final.attempt.publish_post_call_count == 1
    assert final.snapshot.store_binding == pinned
    posts = [call for call in transport.calls if call["method"] == "POST"]
    assert len(posts) == 1
    assert (
        posts[0]["url"]
        == "https://api.printify.com/v1/shops/7/products/merchant_product/publish.json"
    )
    assert all(call["headers"]["Authorization"] == "Bearer " + TOKEN for call in transport.calls)
    assert not system.resolver.legacy_calls


def test_new_account_direct_upload_api_is_denied_before_store_activation():
    system = Workflow(enabled=False)
    adapter = UploadApiAdapter(
        claims_policy=SellerClaimsPolicy(
            issuer=system.account_config.issuer,
            client_id=system.account_config.client_id,
            required_scope="mr-lister-api/seller",
        ),
        uploads=system.uploads,
    )
    response = adapter.handle(
        event(
            system.account_config,
            body=json.dumps(
                {
                    "filename": "merchant-art.png",
                    "content_type": "image/png",
                    "content_sha256": "a" * 64,
                    "size_bytes": 1,
                }
            ),
        )
    )
    assert response["statusCode"] == 403
    assert not system.controls.jobs and not system.artifacts.authorization_calls
    assert not system.secrets.items and not system.shops.requests


@pytest.mark.parametrize("enabled,fail_grant", [(False, False), (True, True)])
def test_disabled_workflow_or_pending_seller_grant_never_authorizes_intake_binding(
    enabled, fail_grant
):
    system = Workflow(enabled=enabled, fail_grant=fail_grant)
    if fail_grant:
        with pytest.raises(ConnectionError):
            system.connect()
    else:
        system.connect()
    setup = system.directory.get_setup(system.account.owner_id)
    assert setup.binding is not None and setup.seller_granted is False
    assert system.directory.get_connection(setup.binding).state == "pending_activation"
    with pytest.raises(ConnectionError):
        system.directory.get_active_binding(
            system.account.owner_id, setup.binding.shop_binding_id, setup.record_version
        )
    enabled_directory = DynamoConnectionDirectory(
        system.connection_dynamo, replace(system.connection_config, workflow_enabled=True)
    )
    with pytest.raises(ConnectionError):
        enabled_directory.get_active_binding(
            system.account.owner_id, setup.binding.shop_binding_id, setup.record_version
        )
    assert not system.controls.jobs and not system.artifacts.authorization_calls


@pytest.mark.parametrize("subject", [PRIMARY_SUBJECT, JUDGE_SUBJECT])
def test_reserved_primary_and_judge_cannot_enroll_manage_connections_or_gain_seller_via_onboarding(
    subject,
):
    system = Workflow()
    calls = len(system.cognito.calls)
    with pytest.raises(ValueError):
        native_signup(confirmation(system.account_config, subject), system.account_config)
    assert len(system.cognito.calls) == calls
    invoked = []
    handler = ConnectionHttpHandler(
        config=system.connection_config,
        account_reader_factory=lambda: invoked.append("account") or system.accounts,
        service_factory=lambda: invoked.append("service") or system.setup,
        allowed_operation="validate",
    )
    reply = handler(
        event(
            system.account_config,
            subject,
            route="POST /v1/connections/printify/validate",
            body=json.dumps({"token": TOKEN}),
        )
    )
    assert reply["statusCode"] == 403 and invoked == []
    assert not system.secrets.items and not system.shops.requests
    assert len(system.cognito.calls) == calls


def test_original_connection_survives_other_selected_store_and_foreign_owner_binding_rejected():
    system = Workflow()
    _, ready = system.connect()
    original = system.binding(ready)
    other = system.signup(SECOND_SUBJECT)
    _, other_ready = system.connect(other, key="second-merchant")
    other_binding = system.binding(other_ready, other)
    assert (
        original.shop_id == other_binding.shop_id
        and original.connection_id != other_binding.connection_id
    )
    with pytest.raises(ConnectionConflict):
        system.directory.get_active_binding(
            system.account.owner_id, other_binding.shop_binding_id, other_ready["record_version"]
        )
    # Simulate a future preference change without altering the original connection row. Current
    # selection is used for new intake; exact job credential resolution ignores that preference.
    replacement = StoreBindingAuthority.create(
        owner_id=system.account.owner_id,
        connection_id="conn_" + "c" * 32,
        shop_binding_id="binding_" + "c" * 32,
        shop_id=99,
        authorization_epoch=1,
    )
    changed = Setup(
        owner_id=system.account.owner_id,
        record_version=ready["record_version"] + 1,
        binding=replacement,
        seller_granted=True,
    )
    system.connection_dynamo.items[setup_key(system.account.owner_id)]["payload"]["S"] = (
        changed.model_dump_json()
    )
    actual = system.resolver.resolve_exact(binding=original)
    assert actual.store_binding == original and actual.shop_id == 7
    resources = OwnerBoundProviderDraftResources(
        connection_resolver=system.resolver,
        s3_client=object(),
        artifact_bucket="integration-private",
        bucket_owner_account_id="123456789012",
    )
    resources.for_binding(owner_id=system.account.owner_id, binding=original).synchronizer(
        owner_id=system.account.owner_id, shop_id=7
    )
    with pytest.raises(PrintifyAuthenticationError):
        resources.for_binding(owner_id=system.account.owner_id, binding=other_binding)
    assert not system.resolver.legacy_calls


def test_exact_publication_credential_rejects_tampered_same_shop_connection_without_fallback():
    system = Workflow()
    _, ready = system.connect()
    pinned = system.binding(ready)
    authority = system.approve_draft(pinned)
    execution, _ = system.request_and_reconstruct(authority)
    provider = execution.provider_authority
    assert provider is not None
    stored = system.directory.get_connection(pinned)
    other = StoreBindingAuthority.create(
        owner_id=pinned.owner_id,
        connection_id="conn_" + "c" * 32,
        shop_binding_id="binding_" + "c" * 32,
        shop_id=pinned.shop_id,
        authorization_epoch=1,
    )
    corrupted = Connection(
        binding=other,
        candidate_id=stored.candidate_id,
        secret=stored.secret,
        store=stored.store,
        state="active",
    )
    system.connection_dynamo.items[connection_key(pinned)]["payload"]["S"] = (
        corrupted.model_dump_json()
    )
    credentials = ProductionPublicationProviderCredentialAuthority(connections=system.resolver)
    reads = system.secrets.reads
    with pytest.raises(PublicationProviderCredentialError):
        credentials.resolve_exact(authority=provider)
    assert system.secrets.reads == reads
    assert not system.resolver.legacy_calls
