import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import phase6Fixtures from "../../contracts/browser/phase6.5.fixtures.json";
import phase7Fixtures from "../../contracts/publication/phase7.0.1.browser.fixtures.json";
import {
  buildDisabledPublicationBrowser,
  frozenPhase7BrowserActivation,
  Phase7BrowserDisabledError,
} from "../offline/phase7/activation";
import {
  PublicationApiError,
  type PublicationApiPort,
  type PublicationDecodedResponse,
} from "../offline/phase7/api-client";
import {
  publicationRequestResponseSchema,
  sellerPublicationProjectionSchema,
  type PublicationRequestResponse,
  type SellerPublicationProjection,
} from "../offline/phase7/contracts";
import { PublicationWorkspace } from "../offline/phase7/PublicationWorkspace";
import { sellerReviewSchema, type SellerReview } from "../src/contracts";

const JOB_ID = "job_phase714_browser";

describe("offline Phase 7 publication browser", () => {
  it("cannot be composed under the frozen exact-disabled activation tuple", () => {
    const build = vi.fn(() => ({ mounted: true }));

    expect(frozenPhase7BrowserActivation).toEqual({
      contract_version: "7.0.1",
      request_enabled: false,
      query_enabled: false,
      publication_enabled: false,
      scaffold_only: true,
    });
    expect(() => buildDisabledPublicationBrowser(build)).toThrow(Phase7BrowserDisabledError);
    expect(build).not.toHaveBeenCalled();
  });

  it("keeps the request control absent unless the offline oracle explicitly supplies authority", async () => {
    const { api, requestPublication } = apiReturning("not_requested");

    render(<PublicationWorkspace jobId={JOB_ID} approvedReview={approvedReview()} api={api} />);

    expect(await screen.findByText("No publication request exists for this approved listing.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Publish this approved listing" })).not.toBeInTheDocument();
    expect(screen.getByText("Seller publication is not activated for this application.")).toBeVisible();
    expect(requestPublication).not.toHaveBeenCalled();
  });

  it("requires explicit acknowledgement and restores focus after keyboard or button cancellation", async () => {
    const user = userEvent.setup();
    const { api, requestPublication } = apiReturning("not_requested");
    render(
      <PublicationWorkspace
        jobId={JOB_ID}
        approvedReview={approvedReview()}
        api={api}
        requestAvailableForOfflineOracle
      />,
    );
    const trigger = await screen.findByRole("button", { name: "Publish this approved listing" });

    await user.click(trigger);
    expect(screen.getByRole("heading", { name: "Publish this exact approved listing?" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Publish exact approved listing" })).toBeDisabled();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Go back" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(requestPublication).not.toHaveBeenCalled();
  });

  it("sends exactly one request and disables confirmation while it is pending", async () => {
    const user = userEvent.setup();
    const pending = deferred<PublicationDecodedResponse<PublicationRequestResponse>>();
    const notRequested = projection("not_requested");
    const queued = projection("queued");
    const getPublication = vi.fn<PublicationApiPort["getPublication"]>()
      .mockResolvedValueOnce(decoded(notRequested))
      .mockResolvedValueOnce(decoded(queued));
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>()
      .mockReturnValue(pending.promise);
    const review = approvedReview();

    render(
      <PublicationWorkspace
        jobId={JOB_ID}
        approvedReview={review}
        api={{ getPublication, requestPublication }}
        requestAvailableForOfflineOracle
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Publish this approved listing" }));
    await user.click(screen.getByRole("checkbox", {
      name: "I understand this is the one publication request for this approved listing.",
    }));
    const confirm = screen.getByRole("button", { name: "Publish exact approved listing" });
    await user.click(confirm);

    expect(requestPublication).toHaveBeenCalledTimes(1);
    expect(requestPublication).toHaveBeenCalledWith(review, expect.stringMatching(
      /^web:publication:8:3:[0-9a-f-]{36}$/u,
    ));
    expect(screen.getByRole("button", { name: "Requesting publication…" })).toBeDisabled();

    await act(async () => {
      pending.resolve(decodedRequest());
      await pending.promise;
    });
    expect(await screen.findByText("Publication is queued for its one bounded attempt.")).toBeVisible();
    expect(requestPublication).toHaveBeenCalledTimes(1);
  });

  it("recovers a strong-authority conflict by GET without silently resubmitting", async () => {
    const user = userEvent.setup();
    const notRequested = projection("not_requested");
    const queued = projection("queued");
    const getPublication = vi.fn<PublicationApiPort["getPublication"]>()
      .mockResolvedValueOnce(decoded(notRequested))
      .mockResolvedValueOnce(decoded(queued));
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>().mockRejectedValue(
      new PublicationApiError(412, "AUTHORITY_CONFLICT", "Authority changed.", "request-conflict", null),
    );
    render(
      <PublicationWorkspace
        jobId={JOB_ID}
        approvedReview={approvedReview()}
        api={{ getPublication, requestPublication }}
        requestAvailableForOfflineOracle
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Publish this approved listing" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Publish exact approved listing" }));

    expect(await screen.findByText(
      "The authoritative publication request already exists. No second request was sent.",
    )).toBeVisible();
    expect(requestPublication).toHaveBeenCalledTimes(1);
    expect(getPublication).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Publish this approved listing" })).not.toBeInTheDocument();
  });

  it.each([
    ["queued", "Publication is queued for its one bounded attempt."],
    ["preflight", "Checking the exact connected Etsy shop and approved product."],
    ["publishing", "The one authorized publication request is being processed."],
    ["verifying", "Printify accepted the request. Verifying the Etsy listing now."],
    ["reconciling", "The provider outcome may be uncertain. Read-only verification is continuing; do not retry."],
  ] as const)("recovers the %s state from durable status before any POST", async (name, expected) => {
    const { api, getPublication, requestPublication } = apiReturning(name);

    render(
      <PublicationWorkspace
        jobId={JOB_ID}
        approvedReview={approvedReview()}
        api={api}
        requestAvailableForOfflineOracle
      />,
    );

    expect(await screen.findByText(expected)).toBeVisible();
    expect(getPublication).toHaveBeenCalledWith(JOB_ID);
    expect(requestPublication).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Publish this approved listing" })).not.toBeInTheDocument();
  });

  it("exposes a canonical link and completion notification only after positive verification", async () => {
    const { api, requestPublication } = apiReturning("published");
    render(<PublicationWorkspace jobId={JOB_ID} approvedReview={approvedReview()} api={api} />);

    expect(await screen.findByText("Your verified Etsy listing is ready.")).toHaveAttribute("role", "status");
    const link = screen.getByRole("link", { name: "Open verified Etsy listing" });
    expect(link).toHaveAttribute("href", "https://www.etsy.com/listing/123456789");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/report_phase714_published/u)).toBeVisible();
    expect(requestPublication).not.toHaveBeenCalled();
  });

  it.each([
    ["failed", "This one-shot request cannot be retried."],
    ["outcome_unknown", "Do not retry this publication request."],
  ] as const)("renders the terminal %s state without retry or result authority", async (name, expected) => {
    const { api, requestPublication } = apiReturning(name);
    render(
      <PublicationWorkspace
        jobId={JOB_ID}
        approvedReview={approvedReview()}
        api={api}
        requestAvailableForOfflineOracle
      />,
    );

    expect(await screen.findByText(new RegExp(expected, "u"))).toBeVisible();
    expect(screen.queryByRole("link", { name: "Open verified Etsy listing" })).not.toBeInTheDocument();
    expect(screen.queryByText("Your verified Etsy listing is ready.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/u })).not.toBeInTheDocument();
    expect(requestPublication).not.toHaveBeenCalled();
  });

  it("offers a read-only retry after a status failure", async () => {
    const user = userEvent.setup();
    const recovered = projection("not_requested");
    const getPublication = vi.fn<PublicationApiPort["getPublication"]>()
      .mockRejectedValueOnce(new Error("Publication status is unavailable."))
      .mockResolvedValueOnce(decoded(recovered));
    const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>();
    render(
      <PublicationWorkspace
        jobId={JOB_ID}
        approvedReview={approvedReview()}
        api={{ getPublication, requestPublication }}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Publication status is unavailable.");
    await user.click(screen.getByRole("button", { name: "Check publication status" }));
    expect(await screen.findByText("No publication request exists for this approved listing.")).toBeVisible();
    expect(getPublication).toHaveBeenCalledTimes(2);
    expect(requestPublication).not.toHaveBeenCalled();
  });
});

function apiReturning(name: keyof typeof phase7Fixtures.projections): {
  api: PublicationApiPort;
  getPublication: ReturnType<typeof vi.fn<PublicationApiPort["getPublication"]>>;
  requestPublication: ReturnType<typeof vi.fn<PublicationApiPort["requestPublication"]>>;
} {
  const getPublication = vi.fn<PublicationApiPort["getPublication"]>()
    .mockResolvedValue(decoded(projection(name)));
  const requestPublication = vi.fn<PublicationApiPort["requestPublication"]>();
  return { api: { getPublication, requestPublication }, getPublication, requestPublication };
}

function projection(name: keyof typeof phase7Fixtures.projections): SellerPublicationProjection {
  return sellerPublicationProjectionSchema.parse(phase7Fixtures.projections[name]);
}

function decoded(value: SellerPublicationProjection): PublicationDecodedResponse<SellerPublicationProjection> {
  return { value, requestId: `request-${value.stage}`, etag: `"${value.etag}"` };
}

function decodedRequest(): PublicationDecodedResponse<PublicationRequestResponse> {
  return {
    value: publicationRequestResponseSchema.parse(phase7Fixtures.publication_request_response),
    requestId: "request-publication",
    etag: null,
  };
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

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolvePromise: ((value: T) => void) | null = null;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: (value) => {
      if (resolvePromise === null) throw new Error("Deferred promise is unavailable");
      resolvePromise(value);
    },
  };
}
