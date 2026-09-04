import type { z } from "zod";
import type { AuthSession } from "../auth/session";
import type { SellerReview } from "../contracts";
import {
  publicationErrorSchema,
  publicationRequestResponseSchema,
  sellerPublicationProjectionSchema,
  type PublicationRequestResponse,
  type SellerPublicationProjection,
} from "./contracts";

export interface PublicationDecodedResponse<T> {
  value: T;
  requestId: string;
  etag: string | null;
}

export interface PublicationApiPort {
  getPublication(jobId: string): Promise<PublicationDecodedResponse<SellerPublicationProjection>>;
  requestPublication(
    review: SellerReview,
    idempotencyKey: string,
  ): Promise<PublicationDecodedResponse<PublicationRequestResponse>>;
}

export class PublicationApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string,
    readonly retryAfterSeconds: number | null,
  ) {
    super(message);
    this.name = "PublicationApiError";
  }

  get isAuthorityConflict(): boolean {
    return this.status === 409 || this.status === 412 || this.status === 428;
  }
}

export class PublicationContractError extends Error {
  constructor(readonly requestId: string) {
    super("The publication API returned an unexpected response.");
    this.name = "PublicationContractError";
  }
}

export class BrowserPublicationApiClient implements PublicationApiPort {
  constructor(
    private readonly session: AuthSession,
    private readonly fetcher: typeof fetch = window.fetch.bind(window),
  ) {}

  getPublication(jobId: string): Promise<PublicationDecodedResponse<SellerPublicationProjection>> {
    return this.request(
      `/v1/jobs/${safeId(jobId)}/publication`,
      { method: "GET" },
      sellerPublicationProjectionSchema,
    ).then((response) => {
      if (response.value.job_id !== jobId || response.etag !== `"${response.value.etag}"`) {
        throw new PublicationContractError(response.requestId);
      }
      return response;
    });
  }

  requestPublication(
    review: SellerReview,
    idempotencyKey: string,
  ): Promise<PublicationDecodedResponse<PublicationRequestResponse>> {
    if (review.display_state !== "approved" || review.stage !== "complete"
      || review.review_version < 1 || review.review_fingerprint === null
      || review.review_authority_etag === null) {
      throw new PublicationContractError("unavailable");
    }
    return this.request(
      `/v1/jobs/${safeId(review.job_id)}/publish`,
      {
        method: "POST",
        headers: {
          "Idempotency-Key": idempotencyKey,
          "If-Match": `"${review.review_authority_etag}"`,
        },
        body: JSON.stringify({
          expected_record_version: review.record_version,
          expected_review_version: review.review_version,
          expected_review_fingerprint: review.review_fingerprint,
          confirmation: "publish_exact_approved_listing",
        }),
      },
      publicationRequestResponseSchema,
    ).then((response) => {
      if (response.value.job_id !== review.job_id
        || response.value.review_version !== review.review_version
        || response.value.record_version <= review.record_version) {
        throw new PublicationContractError(response.requestId);
      }
      return response;
    });
  }

  private async request<T>(
    path: string,
    init: RequestInit,
    schema: z.ZodType<T>,
  ): Promise<PublicationDecodedResponse<T>> {
    if (!path.startsWith("/v1/") || path.startsWith("//")) {
      throw new PublicationContractError("unavailable");
    }
    const initialToken = this.session.getAccessToken() ?? await this.session.renewAccessToken();
    if (initialToken === null) {
      throw new PublicationApiError(
        401,
        "AUTHENTICATION_REQUIRED",
        "Sign in is required to continue.",
        "unavailable",
        null,
      );
    }
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body !== undefined) headers.set("Content-Type", "application/json");
    const perform = (token: string) => {
      const requestHeaders = new Headers(headers);
      requestHeaders.set("Authorization", `Bearer ${token}`);
      return this.fetcher(path, {
        ...init,
        headers: requestHeaders,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
    };
    let response = await perform(initialToken);
    if (response.status === 401) {
      const renewed = await this.session.renewAccessToken(true);
      if (renewed !== null) response = await perform(renewed);
    }
    if (!response.ok) await throwPublicationApiError(response);
    const requestId = response.headers.get("X-Request-Id") ?? "unavailable";
    const text = await response.text();
    if (text.length > 2 * 1024 * 1024) throw new PublicationContractError(requestId);
    let decoded: unknown;
    try {
      decoded = JSON.parse(text) as unknown;
    } catch {
      throw new PublicationContractError(requestId);
    }
    const parsed = schema.safeParse(decoded);
    if (!parsed.success) throw new PublicationContractError(requestId);
    return { value: parsed.data, requestId, etag: response.headers.get("ETag") };
  }
}

async function throwPublicationApiError(response: Response): Promise<never> {
  const requestId = response.headers.get("X-Request-Id") ?? "unavailable";
  const retry = response.headers.get("Retry-After");
  const retryAfterSeconds = retry !== null && /^\d{1,4}$/u.test(retry) ? Number(retry) : null;
  let candidate: unknown;
  try {
    const text = await response.text();
    candidate = text.length <= 65_536 ? JSON.parse(text) as unknown : null;
  } catch {
    candidate = null;
  }
  const parsed = publicationErrorSchema.safeParse(candidate);
  if (!parsed.success) {
    throw new PublicationApiError(
      response.status,
      "UNEXPECTED_RESPONSE",
      "The publication API returned an unexpected response.",
      requestId,
      retryAfterSeconds,
    );
  }
  throw new PublicationApiError(
    response.status,
    parsed.data.error.code,
    parsed.data.error.message,
    requestId,
    retryAfterSeconds,
  );
}

function safeId(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(value)) {
    throw new PublicationContractError("unavailable");
  }
  return encodeURIComponent(value);
}
