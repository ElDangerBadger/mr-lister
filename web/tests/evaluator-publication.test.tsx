import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import phase6Fixtures from "../../contracts/browser/phase6.5.fixtures.json";
import { MemoryAuthSession } from "../src/auth/session";
import { sellerReviewSchema } from "../src/contracts";
import {
  BrowserPublicationApiClient,
  PublicationContractError,
  type PublicationApiPort,
} from "../src/publication/api-client";
import {
  evaluatorPublicationProjectionSchema,
  publicationRequestResponseSchema,
  publicationStatusProjectionSchema,
  sellerPublicationProjectionSchema,
} from "../src/publication/contracts";
import { PublicationWorkspace } from "../src/publication/PublicationWorkspace";

const JOB_ID = "job_evaluator_owned";
const policyMessage = "Publishing is disabled in this evaluation workspace. You can review and approve drafts, "
  + "but this workspace cannot publish Etsy listings.";

function evaluatorStatus() {
  return evaluatorPublicationProjectionSchema.parse({
    contract_version: "evaluator-publication-v1",
    job_id: JOB_ID,
    publication_enabled: false,
    request_enabled: false,
    request_disabled_reason: "EVALUATOR_PUBLICATION_DISABLED",
    request_disabled_message: policyMessage,
    state: "not_requested",
    stage: "awaiting_activation",
    aggregate_record_version: null,
    attempt_status: null,
    verification_deadline: null,
    safe_listing_url: null,
    verified_at: null,
    report_id: null,
    terminal_at: null,
    notification_available: false,
    updated_at: "2026-09-11T12:00:00Z",
    etag: "d".repeat(64),
  });
}

function approvedReview() {
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

function authenticatedSession() {
  const session = new MemoryAuthSession();
  session.set("access-token", 3_600, "refresh-token");
  return session;
}

function statusResponse(value = evaluatorStatus(), etag = `"${value.etag}"`) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { ETag: etag, "X-Request-Id": "evaluator-request" },
  });
}

describe("server-authorized evaluator publication policy", () => {
  it("is a separate read contract and cannot be passed as active 7.1.0 request authority", () => {
    const value = evaluatorStatus();
    expect(publicationStatusProjectionSchema.safeParse(value).success).toBe(true);
    expect(sellerPublicationProjectionSchema.safeParse(value).success).toBe(false);
    expect(publicationRequestResponseSchema.safeParse(value).success).toBe(false);
    expect(publicationStatusProjectionSchema.safeParse({ ...value, contract_version: "7.0.1" }).success).toBe(false);
  });

  it.each([
    ["publication_enabled", true],
    ["request_enabled", true],
    ["request_disabled_reason", "PUBLICATION_NOT_ELIGIBLE"],
    ["request_disabled_message", "Publish now"],
    ["state", "published"],
    ["stage", "publishing"],
    ["safe_listing_url", "https://www.etsy.com/listing/123"],
    ["aggregate_record_version", 1],
    ["notification_available", true],
    ["extra_authority", "publish"],
  ])("rejects evaluator authority drift in %s", (field, value) => {
    expect(publicationStatusProjectionSchema.safeParse({ ...evaluatorStatus(), [field]: value }).success).toBe(false);
  });

  it("uses authenticated GET and validates owner-job response binding and strong ETag", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(statusResponse());
    const client = new BrowserPublicationApiClient(authenticatedSession(), fetcher);
    const response = await client.getPublication(JOB_ID);
    expect(response.value).toEqual(evaluatorStatus());
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]?.[0]).toBe(`/v1/jobs/${JOB_ID}/publication`);
    const request = fetcher.mock.calls[0]?.[1];
    expect(request?.method).toBe("GET");
    expect(new Headers(request?.headers).get("Authorization")).toBe("Bearer access-token");
    expect(request?.cache).toBe("no-store");

    fetcher.mockResolvedValueOnce(statusResponse({ ...evaluatorStatus(), job_id: "another_job" }));
    await expect(client.getPublication(JOB_ID)).rejects.toBeInstanceOf(PublicationContractError);
    fetcher.mockResolvedValueOnce(statusResponse(evaluatorStatus(), '"wrong"'));
    await expect(client.getPublication(JOB_ID)).rejects.toBeInstanceOf(PublicationContractError);
  });

  it("explains workspace policy for an approved draft without a publish action or false failure", async () => {
    const user = userEvent.setup();
    const value = evaluatorStatus();
    const getPublication = vi.fn().mockResolvedValue({ value, requestId: "evaluator-request", etag: `"${value.etag}"` });
    const requestPublication = vi.fn();
    const api: PublicationApiPort = { getPublication, requestPublication };
    render(<PublicationWorkspace jobId={JOB_ID} approvedReview={approvedReview()} api={api} />);
    expect(await screen.findByText(policyMessage)).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /publish.*listing/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/not eligible for publication/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Provider verification continues automatically/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh publication status" }));
    expect(await screen.findByText(/Publication status refreshed/)).toBeVisible();
    expect(getPublication).toHaveBeenCalledTimes(2);
    expect(requestPublication).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
