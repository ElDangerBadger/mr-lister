import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import phase6Fixtures from "../../contracts/browser/phase6.5.fixtures.json";
import phase7Fixtures from "../../contracts/publication/phase7.0.1.browser.fixtures.json";
import { AppRoutes } from "../src/App";
import type { ApiPort } from "../src/api/client";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";
import {
  BrowserPublicationApiClient,
  PublicationContractError,
  type PublicationApiPort,
  type PublicationDecodedResponse,
} from "../src/publication/api-client";
import {
  publicationRequestResponseSchema,
  sellerPublicationProjectionSchema,
  type PublicationRequestResponse,
  type SellerPublicationProjection,
} from "../src/publication/contracts";
import { PublicationWorkspace } from "../src/publication/PublicationWorkspace";

const JOB_ID = "job_phase718_browser";

describe("active Phase 7 publication browser", () => {
  it("mounts the enabled publication workspace only on an authenticated approved review", async () => {
    const review = approvedReview();
    const publicationApi: PublicationApiPort = {
      getPublication: vi.fn().mockResolvedValue(decoded(projection("not_requested"))),
      requestPublication: vi.fn(),
    };
    render(
      <MemoryRouter initialEntries={[`/jobs/${JOB_ID}`]}>
        <AppRoutes dependencies={{ ...sellerDependencies(review), publicationApi }} />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Publication status" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "Publish this approved listing" })).toBeEnabled();
    expect(screen.getByText("Nothing publishes without explicit seller confirmation")).toBeVisible();
  });

  it("uses the exact authenticated GET and authority-bound POST contract", async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation((input, init) => {
      calls.push([input, init]);
      if (calls.length === 1) return Promise.resolve(statusResponse("not_requested"));
      return Promise.resolve(new Response(JSON.stringify(requestResponse()), {
        status: 202,
        headers: { "X-Request-Id": "request-publication" },
      }));
    });
    const client = new BrowserPublicationApiClient(authenticatedSession(), fetcher);
    const review = approvedReview();

    await client.getPublication(JOB_ID);
    await client.requestPublication(review, "web:publication:stable-key");

    expect(calls).toHaveLength(2);
    expect(calls[0]?.[0]).toBe(`/v1/jobs/${JOB_ID}/publication`);
    expect(calls[0]?.[1]?.method).toBe("GET");
    expect(calls[1]?.[0]).toBe(`/v1/jobs/${JOB_ID}/publish`);
    expect(calls[1]?.[1]?.method).toBe("POST");
    const requestBody = calls[1]?.[1]?.body;
    if (typeof requestBody !== "string") throw new Error("Expected the publication body to be JSON");
    expect(JSON.parse(requestBody) as unknown).toEqual({
      expected_record_version: review.record_version,
      expected_review_version: review.review_version,
      expected_review_fingerprint: review.review_fingerprint,
      confirmation: "publish_exact_approved_listing",
    });
    const headers = new Headers(calls[1]?.[1]?.headers);
    expect(headers.get("Authorization")).toBe("Bearer access-token");
    expect(headers.get("If-Match")).toBe(`"${review.review_authority_etag ?? ""}"`);
    expect(headers.get("Idempotency-Key")).toBe("web:publication:stable-key");
    expect(calls[1]?.[1]).toMatchObject({
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  });

  it("accepts only the enabled 7.1.0 capability and coherent one-shot states", () => {
    const available = projection("not_requested");
    expect(available.request_enabled).toBe(true);
    expect(available.request_disabled_reason).toBeNull();
    expect(sellerPublicationProjectionSchema.safeParse({
      ...available,
      publication_enabled: false,
    }).success).toBe(false);
    expect(sellerPublicationProjectionSchema.safeParse({
      ...available,
      request_enabled: false,
      request_disabled_reason: "PUBLICATION_ALREADY_REQUESTED",
    }).success).toBe(false);

    const published = projection("published");
    expect(sellerPublicationProjectionSchema.safeParse({
      ...published,
      safe_listing_url: "https://www.etsy.com.evil.example/listing/123456789",
    }).success).toBe(false);
  });

  it("requires explicit confirmation and sends one request before durable readback", async () => {
    const user = userEvent.setup();
    const notRequested = projection("not_requested");
    const queued = projection("queued");
    const getPublication = vi.fn<PublicationApiPort["getPublication"]>()
      .mockResolvedValueOnce(decoded(notRequested))
      .mockResolvedValue(decoded(queued));
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>()
      .mockResolvedValue(decodedRequest());
    const review = approvedReview();

    render(<PublicationWorkspace
      jobId={JOB_ID}
      approvedReview={review}
      api={{ getPublication, requestPublication }}
    />);

    const trigger = await screen.findByRole("button", { name: "Publish this approved listing" });
    await user.click(trigger);
    expect(screen.getByRole("heading", { name: "Publish this exact approved listing?" })).toHaveFocus();
    const confirm = screen.getByRole("button", { name: "Publish exact approved listing" });
    expect(confirm).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(confirm);

    expect(await screen.findByText("Publication is queued for its one bounded attempt.")).toBeVisible();
    expect(requestPublication).toHaveBeenCalledTimes(1);
    expect(requestPublication).toHaveBeenCalledWith(review, expect.stringMatching(
      /^web:publication:8:3:[0-9a-f-]{36}$/u,
    ));
    expect(getPublication).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Publish this approved listing" })).not.toBeInTheDocument();
  });

  it.each([
    ["published", "Your verified Etsy listing is ready."],
    ["failed", "This one-shot request cannot be retried."],
    ["outcome_unknown", "Do not retry this publication request."],
  ] as const)("renders terminal %s authority without another POST", async (name, expected) => {
    const value = projection(name);
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>();
    render(<PublicationWorkspace
      jobId={JOB_ID}
      approvedReview={approvedReview()}
      api={{ getPublication: vi.fn().mockResolvedValue(decoded(value)), requestPublication }}
    />);

    expect(await screen.findByText(new RegExp(expected, "u"))).toBeVisible();
    expect(screen.queryByRole("button", { name: "Publish this approved listing" })).not.toBeInTheDocument();
    expect(requestPublication).not.toHaveBeenCalled();
    if (name === "published") {
      expect(screen.getByRole("link", { name: "Open verified Etsy listing" })).toHaveAttribute(
        "href",
        "https://www.etsy.com/listing/123456789",
      );
    } else {
      expect(screen.queryByRole("link", { name: "Open verified Etsy listing" })).not.toBeInTheDocument();
    }
  });

  it("restores trigger focus on cancellation and has no detectable accessibility violations", async () => {
    const user = userEvent.setup();
    const value = projection("not_requested");
    const { container } = render(<PublicationWorkspace
      jobId={JOB_ID}
      approvedReview={approvedReview()}
      api={{
        getPublication: vi.fn().mockResolvedValue(decoded(value)),
        requestPublication: vi.fn(),
      }}
    />);
    const trigger = await screen.findByRole("button", { name: "Publish this approved listing" });
    await user.click(trigger);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it("closes and clears confirmation when navigation changes the approval authority", async () => {
    const user = userEvent.setup();
    const firstReview = approvedReview();
    const secondJobId = "job_phase718_browser_second";
    const secondReview = approvedReview(secondJobId);
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>();
    const api: PublicationApiPort = {
      getPublication: vi.fn()
        .mockResolvedValueOnce(decoded(projection("not_requested")))
        .mockResolvedValue(decoded(projection("not_requested", secondJobId))),
      requestPublication,
    };
    const { rerender } = render(<PublicationWorkspace
      jobId={JOB_ID}
      approvedReview={firstReview}
      api={api}
    />);

    await user.click(await screen.findByRole("button", { name: "Publish this approved listing" }));
    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Publish exact approved listing" })).toBeEnabled();

    rerender(<PublicationWorkspace
      jobId={secondJobId}
      approvedReview={firstReview}
      api={api}
    />);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: "Publish this approved listing" })).not.toBeInTheDocument();

    rerender(<PublicationWorkspace
      jobId={secondJobId}
      approvedReview={secondReview}
      api={api}
    />);
    await user.click(await screen.findByRole("button", { name: "Publish this approved listing" }));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Publish exact approved listing" })).toBeDisabled();
    expect(requestPublication).not.toHaveBeenCalled();
  });

  it("does not let an in-flight request for the prior route refresh the next job", async () => {
    const user = userEvent.setup();
    const secondJobId = "job_phase718_browser_second";
    let settleFirstRequest!: (
      value: PublicationDecodedResponse<PublicationRequestResponse>
    ) => void;
    const delayedRequest = new Promise<PublicationDecodedResponse<PublicationRequestResponse>>((resolve) => {
      settleFirstRequest = resolve;
    });
    const getPublication = vi.fn<PublicationApiPort["getPublication"]>((jobId) => Promise.resolve(
      decoded(projection("not_requested", jobId)),
    ));
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>()
      .mockReturnValue(delayedRequest);
    const api = { getPublication, requestPublication };
    const { rerender } = render(<PublicationWorkspace
      jobId={JOB_ID}
      approvedReview={approvedReview()}
      api={api}
    />);

    await user.click(await screen.findByRole("button", { name: "Publish this approved listing" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Publish exact approved listing" }));
    expect(requestPublication).toHaveBeenCalledTimes(1);

    rerender(<PublicationWorkspace
      jobId={secondJobId}
      approvedReview={approvedReview(secondJobId)}
      api={api}
    />);
    expect(await screen.findByRole("button", { name: "Publish this approved listing" })).toBeEnabled();

    settleFirstRequest(decodedRequest());
    await waitFor(() => expect(getPublication).toHaveBeenCalledTimes(2));
    expect(getPublication.mock.calls.map(([jobId]) => jobId)).toEqual([JOB_ID, secondJobId]);
    expect(screen.queryByText(/Publication request accepted/u)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish this approved listing" })).toBeEnabled();
  });

  it("rejects a disabled or wrong-version server response before rendering it", async () => {
    const disabled = {
      ...phase7Fixtures.projections.not_requested,
      job_id: JOB_ID,
    };
    const client = new BrowserPublicationApiClient(
      authenticatedSession(),
      vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(disabled), {
        status: 200,
        headers: { ETag: `"${disabled.etag}"` },
      })),
    );
    await expect(client.getPublication(JOB_ID)).rejects.toBeInstanceOf(PublicationContractError);
  });
});

function authenticatedSession(): MemoryAuthSession {
  const session = new MemoryAuthSession();
  session.set("access-token", 3_600, "refresh-token");
  return session;
}

function sellerDependencies(review: SellerReview): { api: ApiPort; auth: AuthCoordinator } {
  const session = authenticatedSession();
  const never = () => Promise.reject(new Error("Unexpected test call"));
  return {
    api: {
      listJobs: never,
      getJob: never,
      getUpload: never,
      getReview: vi.fn().mockResolvedValue({
        value: review,
        requestId: "request-review",
        etag: `"${review.review_authority_etag ?? ""}"`,
      }),
      createUpload: never,
      authorizeUpload: never,
      completeUpload: never,
      cancelUpload: never,
      reviseListing: never,
      runAction: never,
      fetchArtwork: never,
    },
    auth: {
      session,
      startSignIn: never,
      completeSignIn: never,
      signOut: vi.fn(),
    },
  };
}

function approvedReview(jobId = JOB_ID): SellerReview {
  return sellerReviewSchema.parse({
    ...phase6Fixtures.seller_review_pending,
    job_id: jobId,
    record_version: 8,
    review_version: 3,
    review_fingerprint: "b".repeat(64),
    review_authority_etag: "c".repeat(64),
    display_state: "approved",
    stage: "complete",
  });
}

function projection(
  name: keyof typeof phase7Fixtures.projections,
  jobId = JOB_ID,
): SellerPublicationProjection {
  const source = phase7Fixtures.projections[name];
  const requested = source.state !== "not_requested";
  return sellerPublicationProjectionSchema.parse({
    ...source,
    contract_version: "7.1.0",
    job_id: jobId,
    publication_enabled: true,
    request_enabled: !requested,
    request_disabled_reason: requested ? "PUBLICATION_ALREADY_REQUESTED" : null,
  });
}

function requestResponse(): PublicationRequestResponse {
  return publicationRequestResponseSchema.parse({
    ...phase7Fixtures.publication_request_response,
    contract_version: "7.1.0",
    job_id: JOB_ID,
  });
}

function decoded(value: SellerPublicationProjection): PublicationDecodedResponse<SellerPublicationProjection> {
  return { value, requestId: `request-${value.stage}`, etag: `"${value.etag}"` };
}

function decodedRequest(): PublicationDecodedResponse<PublicationRequestResponse> {
  return { value: requestResponse(), requestId: "request-publication", etag: null };
}

function statusResponse(name: keyof typeof phase7Fixtures.projections): Response {
  const value = projection(name);
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { ETag: `"${value.etag}"`, "X-Request-Id": `request-${name}` },
  });
}
