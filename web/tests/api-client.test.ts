import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { ApiError, BrowserApiClient, ContractError } from "../src/api/client";
import { MemoryAuthSession } from "../src/auth/session";
import { sellerReviewSchema } from "../src/contracts";

describe("BrowserApiClient", () => {
  const previewPath = "/v1/jobs/job_preview/artwork-preview";
  const grantPath = `${previewPath}?format=json`;
  const imageUrl = "https://mr-lister-phase6-artifacts-dev-384627057108-us-west-2.s3.us-west-2.amazonaws.com/private/owners/test-owner/jobs/job_preview/source/source.png?versionId=pinned-version&X-Amz-Signature=test-only";
  const grantResponse = (url = imageUrl) => new Response(JSON.stringify({
    url, expires_at: new Date(Date.now() + 300_000).toISOString(),
  }), { headers: { "Content-Type": "application/json", "X-Request-Id": "request-preview" } });
  const pngResponse = () => new Response(new Uint8Array([137, 80, 78, 71]), {
    headers: { "Content-Type": "image/png" },
  });

  it("clears recent account history with no client-selected owner or cutoff", async () => {
    const session = new MemoryAuthSession();
    session.set("seller-token", 3600, "refresh-token");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify(browserFixtures.clear_recent_jobs), { headers: { "X-Request-Id": "request-clear" } },
    ));
    const response = await new BrowserApiClient(session, fetcher).clearRecentJobs("web:clear:stable");
    expect(response.value).toEqual(browserFixtures.clear_recent_jobs);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [path, options] = fetcher.mock.calls[0]!;
    expect(path).toBe("/v1/jobs/recent/clear");
    expect(options).toMatchObject({ method: "POST", body: "{}", credentials: "omit", cache: "no-store", redirect: "error" });
    expect(new Headers(options?.headers).get("Idempotency-Key")).toBe("web:clear:stable");
    expect(new Headers(options?.headers).get("Authorization")).toBe("Bearer seller-token");
  });

  it("keeps the exact history-clear key and body during authentication renewal", async () => {
    const session = new MemoryAuthSession();
    session.set("expired-token", 3600, "refresh-token");
    session.setRenewer(vi.fn().mockResolvedValue({ accessToken: "renewed-token", expiresInSeconds: 3600 }));
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response("", { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(browserFixtures.clear_recent_jobs)));
    await new BrowserApiClient(session, fetcher).clearRecentJobs("web:clear:stable");
    expect(fetcher.mock.calls).toHaveLength(2);
    for (const [path, options] of fetcher.mock.calls) {
      expect(path).toBe("/v1/jobs/recent/clear");
      expect(options?.body).toBe("{}");
      expect(new Headers(options?.headers).get("Idempotency-Key")).toBe("web:clear:stable");
    }
  });

  it.each([
    {}, { cleared_before: "not-a-date" }, { cleared_before: "2026-09-14T20:00:00Z", owner_id: "unexpected" },
  ])("rejects malformed history-clear acknowledgment: %j", async (body) => {
    await expect(jsonClient(body).clearRecentJobs("web:clear:stable")).rejects.toBeInstanceOf(ContractError);
  });

  it("loads the next bounded history page with the server cursor", async () => {
    const session = new MemoryAuthSession();
    session.set("seller-token", 3600, "refresh-token");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ jobs: [], next_cursor: null })));
    const client = new BrowserApiClient(session, fetcher);
    await client.listJobs("opaque_cursor-2");
    expect(fetcher.mock.calls[0]?.[0]).toBe("/v1/jobs?limit=25&cursor=opaque_cursor-2");
    expect(() => client.listJobs("bad&owner=other")).toThrow("Invalid history cursor");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("fetches an authenticated preview grant then downloads its pinned image without seller credentials", async () => {
    const session = new MemoryAuthSession();
    session.set("seller-token", 3600, "refresh-token");
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(grantResponse())
      .mockResolvedValueOnce(pngResponse());
    const signal = new AbortController().signal;
    const blob = await new BrowserApiClient(session, fetcher).fetchArtwork(previewPath, signal);
    expect(blob.size).toBe(4);
    expect(blob.type).toBe("image/png");
    expect(fetcher).toHaveBeenCalledTimes(2);
    const [grantInput, grantOptions] = fetcher.mock.calls[0]!;
    const [imageInput, imageOptions] = fetcher.mock.calls[1]!;
    expect(grantInput).toBe(grantPath);
    expect(new Headers(grantOptions?.headers).get("Authorization")).toBe("Bearer seller-token");
    expect(new Headers(grantOptions?.headers).get("Accept")).toBe("application/json");
    expect(imageInput).toBe(imageUrl);
    expect(new Headers(imageOptions?.headers).has("Authorization")).toBe(false);
    expect(new Headers(imageOptions?.headers).get("Accept")).toBe("image/png");
    for (const options of [grantOptions, imageOptions]) {
      expect(options).toMatchObject({ method: "GET", cache: "no-store", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer", signal });
    }
  });

  it("renews authentication only for the same-origin preview grant", async () => {
    const session = new MemoryAuthSession();
    session.set("first-token", 3600, "refresh-token");
    const renew = vi.fn().mockResolvedValue({ accessToken: "second-token", expiresInSeconds: 3600 });
    session.setRenewer(renew);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response("", { status: 401 }))
      .mockResolvedValueOnce(grantResponse())
      .mockResolvedValueOnce(pngResponse());
    await new BrowserApiClient(session, fetcher).fetchArtwork(previewPath, new AbortController().signal);
    expect(renew).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls.map(([input]) => input)).toEqual([grantPath, grantPath, imageUrl]);
    expect(new Headers(fetcher.mock.calls[1]?.[1]?.headers).get("Authorization")).toBe("Bearer second-token");
    expect(new Headers(fetcher.mock.calls[2]?.[1]?.headers).has("Authorization")).toBe(false);
  });

  it.each([
    "https://untrusted.example/image.png?versionId=pinned-version",
    imageUrl.replace("https:", "http:"),
    imageUrl.replace("job_preview/source", "job_other/source"),
    imageUrl.replace("versionId=pinned-version&", ""),
    `${imageUrl}&versionId=other-version`,
  ])("rejects an invalid image grant before sending a download request: %s", async (url) => {
    const session = new MemoryAuthSession();
    session.set("seller-token", 3600, "refresh-token");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(grantResponse(url));
    await expect(new BrowserApiClient(session, fetcher).fetchArtwork(previewPath, new AbortController().signal))
      .rejects.toBeInstanceOf(ContractError);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not send or renew seller credentials when the image download is denied", async () => {
    const session = new MemoryAuthSession();
    session.set("seller-token", 3600, "refresh-token");
    const renew = vi.fn();
    session.setRenewer(renew);
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(grantResponse())
      .mockResolvedValueOnce(new Response("Access denied", { status: 403 }));
    await expect(new BrowserApiClient(session, fetcher).fetchArtwork(previewPath, new AbortController().signal)).rejects.toThrow();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(renew).not.toHaveBeenCalled();
    expect(new Headers(fetcher.mock.calls[1]?.[1]?.headers).has("Authorization")).toBe(false);
  });

  it.each([
    { body: new Uint8Array(), type: "image/png" },
    { body: new Uint8Array([1]), type: "text/html" },
    { body: new Uint8Array(5 * 1024 * 1024 + 1), type: "image/png" },
  ])("retains preview content validation: $type ($body.length bytes)", async ({ body, type }) => {
    const session = new MemoryAuthSession();
    session.set("seller-token", 3600, "refresh-token");
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(grantResponse())
      .mockResolvedValueOnce(new Response(body, { headers: { "Content-Type": type } }));
    await expect(new BrowserApiClient(session, fetcher).fetchArtwork(previewPath, new AbortController().signal))
      .rejects.toBeInstanceOf(ContractError);
  });

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

  it.each([500, 502, 503, 504])("keeps a gateway support reference without replaying a failed completion (%s)", async (status) => {
    const session = new MemoryAuthSession();
    session.set("access", 3600, "refresh");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('{"message":"Internal Server Error"}', {
      status, headers: { "X-Amzn-RequestId": "Dq-WejzgPHcESpg=" },
    }));
    const result = new BrowserApiClient(session, fetcher).completeUpload("upload_1", "web:complete:stable");
    await expect(result).rejects.toBeInstanceOf(ApiError);
    await expect(result).rejects.toMatchObject({
      status, requestId: "Dq-WejzgPHcESpg=",
      message: "We couldn't confirm the server's response. Check the current status before trying again.",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("ignores invalid request IDs and raw error bodies, preserving rate-limit guidance", async () => {
    const session = new MemoryAuthSession();
    session.set("access", 3600, "refresh");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("<html>internal details</html>", {
      status: 429, headers: { "X-Request-Id": "x".repeat(129), "X-Amzn-RequestId": "invalid id", "X-Amz-Apigw-Id": "gateway-123=", "Retry-After": "3" },
    }));
    await expect(new BrowserApiClient(session, fetcher).listJobs()).rejects.toMatchObject({
      requestId: "gateway-123=", retryAfterSeconds: 3,
      message: "The service is busy. Wait a moment, then check the current status.",
    });
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
