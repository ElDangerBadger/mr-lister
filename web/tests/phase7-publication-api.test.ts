import { describe, expect, it, vi } from "vitest";
import phase6Fixtures from "../../contracts/browser/phase6.5.fixtures.json";
import phase7Fixtures from "../../contracts/publication/phase7.0.1.browser.fixtures.json";
import {
  OfflinePublicationApiClient,
  PublicationContractError,
} from "../offline/phase7/api-client";
import { MemoryAuthSession } from "../src/auth/session";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

const JOB_ID = "job_phase714_browser";

describe("offline Phase 7 publication API client", () => {
  it("uses the exact authenticated status and one-shot request contracts", async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation((input, init) => {
      calls.push([input, init]);
      if (calls.length === 1) return Promise.resolve(statusResponse("not_requested"));
      return Promise.resolve(new Response(JSON.stringify(phase7Fixtures.publication_request_response), {
        status: 202,
        headers: { "X-Request-Id": "request-publication" },
      }));
    });
    const client = new OfflinePublicationApiClient(authenticatedSession(), fetcher);

    await client.getPublication(JOB_ID);
    await client.requestPublication(approvedReview(), "web:publication:stable-key");

    expect(calls).toHaveLength(2);
    expect(calls[0]?.[0]).toBe(`/v1/jobs/${JOB_ID}/publication`);
    expect(calls[0]?.[1]?.method).toBe("GET");
    expect(calls[1]?.[0]).toBe(`/v1/jobs/${JOB_ID}/publish`);
    expect(calls[1]?.[1]?.method).toBe("POST");
    const requestBody = calls[1]?.[1]?.body;
    expect(typeof requestBody).toBe("string");
    if (typeof requestBody !== "string") throw new Error("Expected the publication body to be JSON");
    expect(JSON.parse(requestBody) as unknown).toEqual(phase7Fixtures.publication_request_body);
    const requestHeaders = new Headers(calls[1]?.[1]?.headers);
    expect(requestHeaders.get("If-Match")).toBe(`"${"c".repeat(64)}"`);
    expect(requestHeaders.get("Idempotency-Key")).toBe("web:publication:stable-key");
    expect(requestHeaders.get("Authorization")).toBe("Bearer access-token");
    for (const [, init] of calls) {
      expect(init?.cache).toBe("no-store");
      expect(init?.credentials).toBe("omit");
      expect(init?.redirect).toBe("error");
      expect(init?.referrerPolicy).toBe("no-referrer");
    }
  });

  it("replays the identical command at most once after memory-only token renewal", async () => {
    const session = authenticatedSession();
    session.set("first-token", 3_600, "refresh-token");
    session.setRenewer(vi.fn().mockResolvedValue({
      accessToken: "second-token",
      expiresInSeconds: 3_600,
    }));
    const calls: RequestInit[] = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_input, init) => {
      calls.push(init ?? {});
      if (calls.length === 1) {
        return Promise.resolve(new Response(JSON.stringify({
          error: { code: "AUTHENTICATION_REQUIRED", message: "Sign in is required to continue." },
        }), { status: 401, headers: { "X-Request-Id": "request-first" } }));
      }
      return Promise.resolve(new Response(JSON.stringify(phase7Fixtures.publication_request_response), {
        status: 202,
        headers: { "X-Request-Id": "request-second" },
      }));
    });
    const client = new OfflinePublicationApiClient(session, fetcher);

    await client.requestPublication(approvedReview(), "web:publication:stable-key");

    expect(calls).toHaveLength(2);
    expect(calls[0]?.body).toBe(calls[1]?.body);
    expect(new Headers(calls[0]?.headers).get("Idempotency-Key")).toBe("web:publication:stable-key");
    expect(new Headers(calls[1]?.headers).get("Idempotency-Key")).toBe("web:publication:stable-key");
    expect(new Headers(calls[0]?.headers).get("Authorization")).toBe("Bearer first-token");
    expect(new Headers(calls[1]?.headers).get("Authorization")).toBe("Bearer second-token");
  });

  it("rejects a mismatched ETag or noncanonical Etsy result before returning it", async () => {
    const badLink = {
      ...phase7Fixtures.projections.published,
      safe_listing_url: "https://www.etsy.com.evil.example/listing/123456789",
    };
    const mismatched = new OfflinePublicationApiClient(
      authenticatedSession(),
      vi.fn<typeof fetch>().mockResolvedValue(new Response(
        JSON.stringify(phase7Fixtures.projections.not_requested),
        { status: 200, headers: { ETag: `"${"f".repeat(64)}"` } },
      )),
    );
    const unsafe = new OfflinePublicationApiClient(
      authenticatedSession(),
      vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(badLink), {
        status: 200,
        headers: { ETag: `"${badLink.etag}"` },
      })),
    );

    await expect(mismatched.getPublication(JOB_ID)).rejects.toBeInstanceOf(PublicationContractError);
    await expect(unsafe.getPublication(JOB_ID)).rejects.toBeInstanceOf(PublicationContractError);
  });
});

function authenticatedSession(): MemoryAuthSession {
  const session = new MemoryAuthSession();
  session.set("access-token", 3_600, "refresh-token");
  return session;
}

function approvedReview(): SellerReview {
  return sellerReviewSchema.parse({
    ...phase6Fixtures.seller_review_pending,
    job_id: JOB_ID,
    record_version: 8,
    review_version: 3,
    review_fingerprint: "b".repeat(64),
    review_authority_etag: "c".repeat(64),
    display_state: "approved",
    stage: "complete",
  });
}

function statusResponse(name: keyof typeof phase7Fixtures.projections): Response {
  const projection = phase7Fixtures.projections[name];
  return new Response(JSON.stringify(projection), {
    status: 200,
    headers: {
      ETag: `"${projection.etag}"`,
      "X-Request-Id": `request-${name}`,
    },
  });
}
