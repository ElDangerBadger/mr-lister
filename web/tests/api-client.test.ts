import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { BrowserApiClient, ContractError } from "../src/api/client";
import { MemoryAuthSession } from "../src/auth/session";
import { sellerReviewSchema } from "../src/contracts";

describe("BrowserApiClient", () => {
  it("replays one exact mutation after memory-only token renewal", async () => {
    const session = new MemoryAuthSession();
    session.set("first-token", 3600, "refresh-token");
    session.setRenewer(vi.fn().mockResolvedValue({ accessToken: "second-token", expiresInSeconds: 3600 }));
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation((input, init) => {
      calls.push([input, init]);
      if (calls.length === 1) {
        return Promise.resolve(new Response(JSON.stringify({ error: { code: "AUTHENTICATION_REQUIRED", message: "Sign in is required to continue.", request_id: "request-1" } }), { status: 401 }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        upload: { upload_id: "upload_1", job_id: "job_1", status: "open", record_version: 0 },
        authorization: null,
      }), { status: 201, headers: { "X-Request-Id": "request-2" } }));
    });
    const client = new BrowserApiClient(session, fetcher);
    const file = new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], "art.png", { type: "image/png" });
    await client.createUpload(file, "a".repeat(64), "web:create:stable-key");
    expect(calls).toHaveLength(2);
    expect(calls[0]?.[0]).toBe("/v1/uploads");
    expect(calls[1]?.[0]).toBe("/v1/uploads");
    expect(calls[0]?.[1]?.body).toBe(calls[1]?.[1]?.body);
    expect(new Headers(calls[0]?.[1]?.headers).get("Idempotency-Key")).toBe("web:create:stable-key");
    expect(new Headers(calls[1]?.[1]?.headers).get("Idempotency-Key")).toBe("web:create:stable-key");
    expect(new Headers(calls[0]?.[1]?.headers).get("Authorization")).toBe("Bearer first-token");
    expect(new Headers(calls[1]?.[1]?.headers).get("Authorization")).toBe("Bearer second-token");
    expect(calls.every(([, init]) => init?.credentials === "omit")).toBe(true);
  });

  it("rejects a recovery projection for a different upload route", async () => {
    const client = jsonClient(browserFixtures.upload_recovery);
    await expect(client.getUpload("upload_other")).rejects.toBeInstanceOf(ContractError);
  });

  it("rejects method-incoherent upload mutation receipts", async () => {
    const open = {
      upload: { upload_id: "upload_1", job_id: "job_1", status: "open", record_version: 1 },
      authorization: null,
    };
    await expect(jsonClient(open).completeUpload("upload_1", "web:complete:stable")).rejects.toBeInstanceOf(ContractError);
    await expect(jsonClient(open).cancelUpload("upload_1", "web:cancel:stable")).rejects.toBeInstanceOf(ContractError);
  });

  it("rejects a command receipt for a different job authority", async () => {
    const review = sellerReviewSchema.parse(browserFixtures.seller_review_pending);
    const client = jsonClient({ job_id: "job_other", state: "cancel_requested", record_version: 2, review_version: 0 });
    await expect(client.runAction(review, "cancel_job", "web:cancel:stable")).rejects.toBeInstanceOf(ContractError);
  });
});

function jsonClient(body: unknown): BrowserApiClient {
  const session = new MemoryAuthSession();
  session.set("access", 3600, "refresh");
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(body), {
    status: 200,
    headers: { "X-Request-Id": "request-contract" },
  }));
  return new BrowserApiClient(session, fetcher);
}
