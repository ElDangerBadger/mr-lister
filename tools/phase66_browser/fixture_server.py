"""Local, credential-free fixture server for the Phase 6.6 browser matrix."""

from __future__ import annotations

import copy
import json
import mimetypes
import threading
import time
from dataclasses import dataclass, field
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DIST_ROOT = REPOSITORY_ROOT / "web" / "dist"
CONTRACT_FIXTURES = REPOSITORY_ROOT / "contracts" / "browser" / "phase6.5.fixtures.json"
ARTWORK_PATH = REPOSITORY_ROOT / "tests" / "evaluation" / "assets" / "transparent_moon_moth.png"
MOCKUP_PATH = REPOSITORY_ROOT / "tests" / "evaluation" / "assets" / "illustrated_badger_subject.png"

PUBLIC_ORIGIN = "https://seller.example.com"
COGNITO_ORIGIN = "https://phase66.auth.us-west-2.amazoncognito.com"
ACCESS_TOKEN = "phase66-access-token"
REQUEST_ID = "request-phase66-browser"

READY_JOB = "job_browser_fixture"
ROUTE_A_JOB = "job_route_a"
ROUTE_B_JOB = "job_route_b"
POLLING_JOB = "job_polling"


def _load_pending_review() -> dict[str, Any]:
    fixtures = json.loads(CONTRACT_FIXTURES.read_text(encoding="utf-8"))
    return fixtures["seller_review_pending"]


def _fingerprint(job_id: str) -> str:
    return sha256(f"phase66:{job_id}".encode()).hexdigest()


def _ready_review(job_id: str, title: str) -> dict[str, Any]:
    review = copy.deepcopy(_load_pending_review())
    fingerprint = _fingerprint(job_id)
    review.update(
        {
            "job_id": job_id,
            "record_version": 7,
            "review_version": 2,
            "review_fingerprint": fingerprint,
            "review_authority_etag": fingerprint,
            "display_state": "ready_for_review",
            "stage": "human_review",
            "updated_at": "2026-08-22T12:04:00Z",
        }
    )
    enabled = {"edit_listing", "approve_review", "cancel_job", "refresh_economics"}
    review["actions"] = [
        {
            **action,
            "enabled": action["action"] in enabled,
            "reason": "AVAILABLE" if action["action"] in enabled else "RETRY_NOT_AVAILABLE",
            "message": (
                "Available for this exact review."
                if action["action"] in enabled
                else "Retry is not available for a review-ready preparation."
            ),
        }
        for action in review["actions"]
    ]
    review["preview"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "url": f"{PUBLIC_ORIGIN}/v1/jobs/{job_id}/artwork-preview",
        "expires_at": "2030-08-22T12:05:00Z",
    }
    review["artwork"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "subject": "Moon moth and botanical stars",
        "visual_elements": ["moon moth", "wildflowers", "stars"],
        "styles": ["vintage engraving"],
        "themes": ["night garden"],
        "visible_text": [],
        "safety_notes": [],
        "confidence": 0.97,
    }
    review["listing"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "title": title,
        "description": "A carefully prepared moon moth and botanical shirt design.",
        "tags": [
            "moon moth",
            "botanical shirt",
            "night garden",
            "nature lover",
            "vintage moth",
            "celestial tee",
            "wildflower art",
            "moth graphic",
            "garden shirt",
            "engraving style",
            "starry nature",
            "gift for gardener",
            "unisex tee",
        ],
        "audience": ["Nature lovers", "Gardeners"],
    }
    review["validation"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "passed": True,
        "issues": [],
    }
    review["synchronization"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "product_id": f"printify_{job_id}",
        "synchronized_at": "2026-08-22T12:03:00Z",
        "review_version": 2,
        "editable_draft": True,
    }
    review["mockups"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "items": [
            {
                "contract_version": "2.0.0",
                "url": f"https://images.printify.com/phase66/{job_id}.png",
                "alt_text": "Black shirt with moon moth artwork",
            }
        ],
    }
    review["economics"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "currency": "USD",
        "label": "Estimated proceeds",
        "minimum_cents": 1025,
        "maximum_cents": 1025,
        "variants": [
            {
                "contract_version": "2.0.0",
                "color": "Black",
                "size": "S",
                "retail_price_cents": 2999,
                "buyer_shipping_cents": 0,
                "production_cost_cents": 1200,
                "production_shipping_cents": 475,
                "marketplace_fees_cents": 299,
                "estimated_proceeds_cents": 1025,
            }
        ],
        "calculated_at": "2026-08-22T12:03:00Z",
        "fresh_until": "2030-08-22T12:03:00Z",
        "production_cost_source": "Connected production product readback",
        "production_cost_observed_at": "2026-08-22T12:02:00Z",
        "production_shipping_source": "Connected production standard US shipping",
        "production_shipping_observed_at": "2026-08-22T12:02:00Z",
        "fee_policy_source": "Etsy US standard fee policy",
        "fee_policy_id": "etsy-us-standard-v1",
        "fee_policy_verified_on": "2026-08-22",
        "assumptions": ["One item purchased", "No advertising fee"],
    }
    review["strands"] = {
        "contract_version": "2.0.0",
        "readiness": "ready",
        "framework": "strands-agents",
        "agent_id": "mr-lister-preparation",
        "prepared_review_version": 2,
        "correlation_id": sha256(job_id.encode()).hexdigest()[:24],
        "tool_calls": ["record_prepared_review"],
        "completed_at": "2026-08-22T12:01:00Z",
    }
    return review


def _polling_review() -> dict[str, Any]:
    review = copy.deepcopy(_load_pending_review())
    review["job_id"] = POLLING_JOB
    review["updated_at"] = "2026-08-22T12:00:00Z"
    return review


REVIEWS = {
    READY_JOB: _ready_review(READY_JOB, "Moonlit botanical moth shirt"),
    ROUTE_A_JOB: _ready_review(ROUTE_A_JOB, "Delayed route A artwork"),
    ROUTE_B_JOB: _ready_review(ROUTE_B_JOB, "Current route B artwork"),
    POLLING_JOB: _polling_review(),
}


def _approved_review() -> dict[str, Any]:
    review = copy.deepcopy(REVIEWS[READY_JOB])
    review.update(
        {
            "record_version": 8,
            "review_version": 3,
            "review_authority_etag": sha256(f"phase66:{READY_JOB}:approved".encode()).hexdigest(),
            "display_state": "approved",
            "stage": "complete",
            "updated_at": "2026-08-22T12:05:00Z",
        }
    )
    review["actions"] = [
        {
            **action,
            "enabled": False,
            "reason": (
                "RETRY_NOT_AVAILABLE" if action["action"] == "retry_job" else "NOT_IN_CURRENT_STATE"
            ),
            "message": "This action is not available after approval.",
        }
        for action in review["actions"]
    ]
    return review


APPROVED_REVIEW = _approved_review()


def _progress(review: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "contract_version",
        "job_id",
        "record_version",
        "review_version",
        "display_state",
        "stage",
        "authority_notice",
        "actions",
        "failure",
        "provider_outcome_unconfirmed",
        "created_at",
        "updated_at",
    )
    return {key: copy.deepcopy(review[key]) for key in keys}


@dataclass
class FixtureState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    approval_attempts: int = 0
    approval_if_match_valid: bool = True
    approval_idempotency_present: bool = True
    api_authorization_valid: bool = True
    provider_transport_attempts: int = 0
    approval_committed: bool = False
    stale_review_reads_remaining: int = 0
    progress_requests: dict[str, int] = field(default_factory=dict)
    review_requests: dict[str, int] = field(default_factory=dict)
    request_log: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        with self.lock:
            self.approval_attempts = 0
            self.approval_if_match_valid = True
            self.approval_idempotency_present = True
            self.api_authorization_valid = True
            self.provider_transport_attempts = 0
            self.approval_committed = False
            self.stale_review_reads_remaining = 0
            self.progress_requests.clear()
            self.review_requests.clear()
            self.request_log.clear()

    def record_api(self, method: str, path: str, authorized: bool) -> None:
        with self.lock:
            self.api_authorization_valid = self.api_authorization_valid and authorized
            self.request_log.append(
                {"method": method, "path": path, "time_ms": time.time_ns() // 1_000_000}
            )

    def review_projection(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            self.review_requests[job_id] = self.review_requests.get(job_id, 0) + 1
            if job_id == READY_JOB and self.approval_committed:
                if self.stale_review_reads_remaining:
                    self.stale_review_reads_remaining -= 1
                    return copy.deepcopy(REVIEWS[job_id])
                return copy.deepcopy(APPROVED_REVIEW)
            return copy.deepcopy(REVIEWS[job_id])

    def record_progress(self, job_id: str) -> None:
        with self.lock:
            self.progress_requests[job_id] = self.progress_requests.get(job_id, 0) + 1

    def record_approval(self, if_match: str | None, idempotency_key: str | None) -> None:
        with self.lock:
            self.approval_attempts += 1
            self.approval_committed = True
            self.stale_review_reads_remaining = 1
            self.approval_if_match_valid = self.approval_if_match_valid and (
                if_match == f'"{_fingerprint(READY_JOB)}"'
            )
            self.approval_idempotency_present = self.approval_idempotency_present and bool(
                idempotency_key and idempotency_key.startswith("web:approve_review:")
            )

    def record_provider_transport_attempt(self) -> None:
        with self.lock:
            self.provider_transport_attempts += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "approval_attempts": self.approval_attempts,
                "approval_if_match_valid": self.approval_if_match_valid,
                "approval_idempotency_present": self.approval_idempotency_present,
                "api_authorization_valid": self.api_authorization_valid,
                "provider_transport_attempts": self.provider_transport_attempts,
                "approval_committed": self.approval_committed,
                "progress_requests": dict(self.progress_requests),
                "review_requests": dict(self.review_requests),
                "request_log": copy.deepcopy(self.request_log),
            }


class Phase66FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]):
        super().__init__(address, Phase66FixtureHandler)
        self.state = FixtureState()


class Phase66FixtureHandler(BaseHTTPRequestHandler):
    server: Phase66FixtureServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        # Firefox and WebKit reuse the OAuth token connection for later routed
        # requests.  Consume every request body before replying so unread form
        # bytes cannot be parsed as the next HTTP/1.1 request line.
        if method in {"POST", "PUT"}:
            self._discard_request_body()
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/__fixture__/health":
            self._json({"status": "ready", "public_origin": PUBLIC_ORIGIN})
            return
        if path == "/__fixture__/state":
            self._json(self.server.state.snapshot())
            return
        if path == "/__fixture__/reset" and method == "POST":
            self.server.state.reset()
            self._json({"status": "reset"})
            return
        if path == "/__fixture__/provider-transport-attempt" and method == "POST":
            self.server.state.record_provider_transport_attempt()
            self._json({"status": "recorded"})
            return
        if path == "/runtime-config.json":
            self._json(
                {
                    "cognito_authorize_url": f"{COGNITO_ORIGIN}/oauth2/authorize",
                    "cognito_token_url": f"{COGNITO_ORIGIN}/oauth2/token",
                    "cognito_logout_url": f"{COGNITO_ORIGIN}/logout",
                    "client_id": "phase66-browser-acceptance",
                    "redirect_uri": f"{PUBLIC_ORIGIN}/auth/callback",
                    "scopes": ["openid", "mr-lister-api/seller"],
                }
            )
            return
        if path == "/oauth2/authorize":
            self._send(
                HTTPStatus.OK,
                b"<!doctype html><title>Managed sign-in fixture</title>"
                b"<h1>Managed sign-in fixture</h1>",
                "text/html; charset=utf-8",
            )
            return
        if path == "/oauth2/token" and method == "POST":
            self._json(
                {
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": "phase66-refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )
            return
        if path == "/logout":
            self._send(HTTPStatus.OK, b"<!doctype html><h1>Signed out fixture</h1>", "text/html")
            return
        if path.startswith("/phase66/") and path.endswith(".png"):
            self._send(HTTPStatus.OK, MOCKUP_PATH.read_bytes(), "image/png")
            return
        if path.startswith("/v1/"):
            self._api(method, path)
            return
        self._static(path)

    def _discard_request_body(self) -> None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0 or length > 1_048_576:
            self.close_connection = True
            return
        if length:
            self.rfile.read(length)

    def _api(self, method: str, path: str) -> None:
        authorized = self.headers.get("Authorization") == f"Bearer {ACCESS_TOKEN}"
        self.server.state.record_api(method, path, authorized)
        if not authorized:
            self._error(HTTPStatus.UNAUTHORIZED, "AUTHENTICATION_REQUIRED", "Sign in is required.")
            return
        if path == "/v1/jobs" and method == "GET":
            jobs = [
                {
                    "job_id": job_id,
                    "state": "awaiting_approval" if job_id != POLLING_JOB else "analyzing_artwork",
                    "record_version": review["record_version"],
                    "review_version": review["review_version"],
                    "created_at": review["created_at"],
                    "updated_at": review["updated_at"],
                }
                for job_id, review in REVIEWS.items()
            ]
            self._json({"jobs": jobs, "next_cursor": None})
            return
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v1", "jobs"]:
            job_id = parts[2]
            review = REVIEWS.get(job_id)
            if review is None:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "The preparation was not found.")
                return
            if len(parts) == 4 and parts[3] == "review" and method == "GET":
                projected_review = self.server.state.review_projection(job_id)
                if job_id == ROUTE_A_JOB:
                    time.sleep(0.8)
                headers = {}
                if projected_review["review_authority_etag"] is not None:
                    headers["ETag"] = f'"{projected_review["review_authority_etag"]}"'
                self._json(projected_review, extra_headers=headers)
                return
            if len(parts) == 3 and method == "GET":
                self.server.state.record_progress(job_id)
                self._json(_progress(review))
                return
            if len(parts) == 4 and parts[3] == "artwork-preview" and method == "GET":
                self._send(HTTPStatus.OK, ARTWORK_PATH.read_bytes(), "image/png")
                return
            if (
                len(parts) == 4
                and parts[3] == "approve"
                and method == "POST"
                and job_id == READY_JOB
            ):
                self.server.state.record_approval(
                    self.headers.get("If-Match"), self.headers.get("Idempotency-Key")
                )
                time.sleep(0.8)
                self._json(
                    {
                        "job_id": READY_JOB,
                        "state": "approved",
                        "record_version": 8,
                        "review_version": 3,
                    }
                )
                return
        self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "The seller route was not found.")

    def _static(self, path: str) -> None:
        relative = path.lstrip("/")
        candidate = (DIST_ROOT / relative).resolve()
        if relative and candidate.is_relative_to(DIST_ROOT.resolve()) and candidate.is_file():
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)
            return
        index = DIST_ROOT / "index.html"
        if index.is_file():
            self._send(HTTPStatus.OK, index.read_bytes(), "text/html; charset=utf-8")
            return
        self._error(
            HTTPStatus.SERVICE_UNAVAILABLE, "BUNDLE_MISSING", "Build the seller bundle first."
        )

    def _json(
        self,
        value: object,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send(
            status,
            json.dumps(value, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
            extra_headers,
        )

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message, "request_id": REQUEST_ID}}, status)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", REQUEST_ID)
            self.send_header("Access-Control-Allow-Origin", PUBLIC_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization,Content-Type,Idempotency-Key,If-Match",
            )
            self.send_header("Vary", "Origin")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def create_server() -> Phase66FixtureServer:
    """Create an ephemeral loopback server without starting its worker thread."""

    return Phase66FixtureServer(("127.0.0.1", 0))
