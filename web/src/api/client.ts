import type { z } from "zod";
import {
  commandResponseSchema,
  errorEnvelopeSchema,
  jobPageSchema,
  jobProgressSchema,
  sellerReviewSchema,
  uploadResponseSchema,
  uploadRecoverySchema,
  type CommandResponse,
  type JobPage,
  type JobProgress,
  type SellerAction,
  type SellerReview,
  type UploadResponse,
  type UploadRecovery,
} from "../contracts";
import type { AuthSession } from "../auth/session";

export interface DecodedResponse<T> {
  value: T;
  requestId: string;
  etag: string | null;
}

export interface ListingDraft {
  title: string;
  description: string;
  tags: string[];
}

export interface ApiPort {
  listJobs(): Promise<DecodedResponse<JobPage>>;
  getJob(jobId: string): Promise<DecodedResponse<JobProgress>>;
  getUpload(uploadId: string): Promise<DecodedResponse<UploadRecovery>>;
  getReview(jobId: string): Promise<DecodedResponse<SellerReview>>;
  createUpload(file: File, sha256: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>>;
  authorizeUpload(uploadId: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>>;
  completeUpload(uploadId: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>>;
  cancelUpload(uploadId: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>>;
  reviseListing(review: SellerReview, listing: ListingDraft, idempotencyKey: string): Promise<DecodedResponse<CommandResponse>>;
  runAction(review: SellerReview, action: Exclude<SellerAction, "edit_listing">, idempotencyKey: string): Promise<DecodedResponse<CommandResponse>>;
  fetchArtwork(previewUrl: string, signal: AbortSignal): Promise<Blob>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string,
    readonly retryAfterSeconds: number | null,
    readonly fields: ReadonlyArray<{ path: string; code: string; message: string }> = [],
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isConflict(): boolean {
    return this.status === 409 || this.status === 412 || this.status === 428;
  }
}

export class ContractError extends Error {
  constructor(readonly requestId: string) {
    super("The seller API returned an unexpected response.");
    this.name = "ContractError";
  }
}

export class BrowserApiClient implements ApiPort {
  constructor(
    private readonly session: AuthSession,
    private readonly fetcher: typeof fetch = window.fetch.bind(window),
  ) {}

  listJobs(): Promise<DecodedResponse<JobPage>> {
    return this.request("/v1/jobs?limit=25", { method: "GET" }, jobPageSchema);
  }

  getJob(jobId: string): Promise<DecodedResponse<JobProgress>> {
    return this.request(`/v1/jobs/${safeId(jobId)}`, { method: "GET" }, jobProgressSchema)
      .then((response) => requireJobResponse(response, jobId));
  }

  getUpload(uploadId: string): Promise<DecodedResponse<UploadRecovery>> {
    return this.request(`/v1/uploads/${safeId(uploadId)}`, { method: "GET" }, uploadRecoverySchema)
      .then((response) => {
        if (response.value.upload_id !== uploadId) throw new ContractError(response.requestId);
        return response;
      });
  }

  getReview(jobId: string): Promise<DecodedResponse<SellerReview>> {
    return this.request(`/v1/jobs/${safeId(jobId)}/review`, { method: "GET" }, sellerReviewSchema)
      .then((response) => {
        if (response.value.job_id !== jobId) throw new ContractError(response.requestId);
        const expected = response.value.review_authority_etag;
        const expectedHeader = expected === null ? null : `"${expected}"`;
        if (response.etag !== expectedHeader) {
          throw new ContractError(response.requestId);
        }
        return response;
      });
  }

  createUpload(file: File, sha256: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>> {
    return this.request("/v1/uploads", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        filename: file.name,
        content_type: "image/png",
        content_sha256: sha256,
        size_bytes: file.size,
      }),
    }, uploadResponseSchema).then((response) => requireUploadMutation(response, null, "open", "optional"));
  }

  authorizeUpload(uploadId: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>> {
    return this.request(`/v1/uploads/${safeId(uploadId)}/authorize`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }, uploadResponseSchema).then((response) => requireUploadMutation(response, uploadId, "open", "optional"));
  }

  completeUpload(uploadId: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>> {
    return this.request(`/v1/uploads/${safeId(uploadId)}/complete`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }, uploadResponseSchema).then((response) => requireUploadMutation(response, uploadId, "completed", "forbidden"));
  }

  cancelUpload(uploadId: string, idempotencyKey: string): Promise<DecodedResponse<UploadResponse>> {
    return this.request(`/v1/uploads/${safeId(uploadId)}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }, uploadResponseSchema).then((response) => requireUploadMutation(response, uploadId, "cancelled", "forbidden"));
  }

  reviseListing(review: SellerReview, listing: ListingDraft, idempotencyKey: string): Promise<DecodedResponse<CommandResponse>> {
    return this.request(`/v1/jobs/${safeId(review.job_id)}/review/listing`, {
      method: "PUT",
      headers: this.reviewHeaders(review, idempotencyKey),
      body: JSON.stringify({
        expected_record_version: review.record_version,
        expected_review_version: review.review_version,
        expected_review_fingerprint: requiredReviewFingerprint(review),
        listing,
      }),
    }, commandResponseSchema).then((response) => requireJobResponse(response, review.job_id));
  }

  runAction(
    review: SellerReview,
    action: Exclude<SellerAction, "edit_listing">,
    idempotencyKey: string,
  ): Promise<DecodedResponse<CommandResponse>> {
    const suffix: Record<typeof action, string> = {
      approve_review: "approve",
      cancel_job: "cancel",
      retry_job: "retry",
      refresh_economics: "economics/refresh",
    };
    const reviewBound = action === "approve_review" || action === "refresh_economics";
    const body = reviewBound ? {
      expected_record_version: review.record_version,
      expected_review_version: review.review_version,
      expected_review_fingerprint: requiredReviewFingerprint(review),
    } : { expected_record_version: review.record_version };
    const headers = reviewBound
      ? this.reviewHeaders(review, idempotencyKey)
      : { "Idempotency-Key": idempotencyKey };
    return this.request(`/v1/jobs/${safeId(review.job_id)}/${suffix[action]}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    }, commandResponseSchema).then((response) => requireJobResponse(response, review.job_id));
  }

  async fetchArtwork(previewUrl: string, signal: AbortSignal): Promise<Blob> {
    const url = new URL(previewUrl, window.location.origin);
    if (url.origin !== window.location.origin
      || !/^\/v1\/jobs\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\/artwork-preview$/u.test(url.pathname)
      || url.search !== ""
      || url.hash !== "") {
      throw new ContractError("unavailable");
    }
    const token = this.session.getAccessToken() ?? await this.session.renewAccessToken();
    if (token === null) throw new ApiError(401, "AUTHENTICATION_REQUIRED", "Sign in is required to continue.", "unavailable", null);
    const fetchPreview = (accessToken: string) => this.fetcher(url.pathname, {
        method: "GET",
        headers: { Authorization: `Bearer ${accessToken}`, Accept: "image/png" },
        cache: "no-store",
        credentials: "omit",
        redirect: "follow",
        referrerPolicy: "no-referrer",
        signal,
      });
    let response = await fetchPreview(token);
    if (response.status === 401) {
      const renewed = await this.session.renewAccessToken(true);
      if (renewed !== null) response = await fetchPreview(renewed);
    }
    if (!response.ok) await throwApiError(response);
    const blob = await response.blob();
    if (blob.size === 0 || blob.size > 5 * 1024 * 1024 || blob.type !== "image/png") {
      throw new ContractError(response.headers.get("X-Request-Id") ?? "unavailable");
    }
    return blob;
  }

  private reviewHeaders(review: SellerReview, idempotencyKey: string): Record<string, string> {
    const etag = review.review_authority_etag;
    if (etag === null) throw new ContractError("unavailable");
    return { "Idempotency-Key": idempotencyKey, "If-Match": `"${etag}"` };
  }

  private async request<T>(path: string, init: RequestInit, schema: z.ZodType<T>): Promise<DecodedResponse<T>> {
    if (!path.startsWith("/v1/") || path.startsWith("//")) throw new Error("API paths must be same-origin /v1 routes");
    const token = this.session.getAccessToken() ?? await this.session.renewAccessToken();
    if (token === null) throw new ApiError(401, "AUTHENTICATION_REQUIRED", "Sign in is required to continue.", "unavailable", null);
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    headers.set("Authorization", `Bearer ${token}`);
    if (init.body !== undefined) headers.set("Content-Type", "application/json");
    const perform = (accessToken: string) => {
      const requestHeaders = new Headers(headers);
      requestHeaders.set("Authorization", `Bearer ${accessToken}`);
      return this.fetcher(path, {
        ...init,
        headers: requestHeaders,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
    };
    let response = await perform(token);
    if (response.status === 401) {
      const renewed = await this.session.renewAccessToken(true);
      if (renewed !== null) response = await perform(renewed);
    }
    if (!response.ok) await throwApiError(response);
    const requestId = response.headers.get("X-Request-Id") ?? "unavailable";
    const text = await response.text();
    if (text.length > 2 * 1024 * 1024) throw new ContractError(requestId);
    let decoded: unknown;
    try {
      decoded = JSON.parse(text) as unknown;
    } catch {
      throw new ContractError(requestId);
    }
    const parsed = schema.safeParse(decoded);
    if (!parsed.success) throw new ContractError(requestId);
    return { value: parsed.data, requestId, etag: response.headers.get("ETag") };
  }
}

export function newIdempotencyKey(operation: string): string {
  const safeOperation = operation.replace(/[^A-Za-z0-9._:-]/gu, "-").slice(0, 48) || "operation";
  return `web:${safeOperation}:${crypto.randomUUID()}`;
}

async function throwApiError(response: Response): Promise<never> {
  const headerRequestId = response.headers.get("X-Request-Id") ?? "unavailable";
  const retry = response.headers.get("Retry-After");
  const retryAfterSeconds = retry !== null && /^\d{1,4}$/u.test(retry) ? Number(retry) : null;
  let candidate: unknown;
  try {
    const text = await response.text();
    candidate = text.length <= 65_536 ? JSON.parse(text) as unknown : null;
  } catch {
    candidate = null;
  }
  const parsed = errorEnvelopeSchema.safeParse(candidate);
  if (!parsed.success) throw new ApiError(response.status, "UNEXPECTED_RESPONSE", "The seller API returned an unexpected response.", headerRequestId, retryAfterSeconds);
  throw new ApiError(
    response.status,
    parsed.data.error.code,
    parsed.data.error.message,
    parsed.data.error.request_id,
    retryAfterSeconds,
    parsed.data.error.fields ?? [],
  );
}

function requiredReviewFingerprint(review: SellerReview): string {
  if (review.review_version < 1 || review.review_fingerprint === null) throw new ContractError("unavailable");
  return review.review_fingerprint;
}

function safeId(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(value)) throw new Error("Invalid resource identifier");
  return encodeURIComponent(value);
}

function requireJobResponse<T extends { job_id: string }>(response: DecodedResponse<T>, expectedJobId: string): DecodedResponse<T> {
  if (response.value.job_id !== expectedJobId) throw new ContractError(response.requestId);
  return response;
}

function requireUploadMutation(
  response: DecodedResponse<UploadResponse>,
  expectedUploadId: string | null,
  expectedStatus: UploadResponse["upload"]["status"],
  authorization: "optional" | "required" | "forbidden",
): DecodedResponse<UploadResponse> {
  if ((expectedUploadId !== null && response.value.upload.upload_id !== expectedUploadId)
    || response.value.upload.status !== expectedStatus
    || (authorization === "required" && response.value.authorization === null)
    || (authorization === "forbidden" && response.value.authorization !== null)) {
    throw new ContractError(response.requestId);
  }
  return response;
}
