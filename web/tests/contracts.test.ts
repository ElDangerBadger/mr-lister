import { describe, expect, it } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import {
  errorEnvelopeSchema,
  jobPageSchema,
  jobProgressSchema,
  sellerReviewSchema,
  runtimeConfigSchemaForOrigin,
  uploadResponseSchema,
  uploadRecoverySchema,
} from "../src/contracts";

describe("Python-to-browser golden contracts", () => {
  it("strictly decodes every authoritative representative fixture", () => {
    expect(jobProgressSchema.parse(browserFixtures.job_progress).job_id).toBe("job_browser_fixture");
    expect(uploadRecoverySchema.parse(browserFixtures.upload_recovery).status).toBe("open");
    expect(sellerReviewSchema.parse(browserFixtures.seller_review_pending).authority_notice).toBe("Unpublished — not on Etsy");
    expect(errorEnvelopeSchema.parse(browserFixtures.validation_error).error.fields).toHaveLength(1);
  });

  it("rejects an expanded seller action surface", () => {
    const fixture = structuredClone(jobProgressSchema.parse(browserFixtures.job_progress));
    const first = fixture.actions[0];
    if (first === undefined) throw new Error("Fixture action is missing");
    fixture.actions.push({ ...first, action: "edit_listing" });
    expect(jobProgressSchema.safeParse(fixture).success).toBe(false);
  });

  it("rejects job pages above the server's 100-item bound", () => {
    const progress = jobProgressSchema.parse(browserFixtures.job_progress);
    const jobs = Array.from({ length: 101 }, (_, index) => ({
      job_id: `job_${index}`,
      state: "intake_validated" as const,
      record_version: progress.record_version,
      review_version: progress.review_version,
      created_at: progress.created_at,
      updated_at: progress.updated_at,
    }));
    expect(jobPageSchema.safeParse({ jobs: jobs.slice(0, 100), next_cursor: null }).success).toBe(true);
    expect(jobPageSchema.safeParse({ jobs, next_cursor: null }).success).toBe(false);
  });

  it("rejects storage authority in upload recovery", () => {
    const fixture = { ...browserFixtures.upload_recovery, object_key: "private/secret.png" };
    expect(uploadRecoverySchema.safeParse(fixture).success).toBe(false);
  });

  it("accepts only the exact deployed Cognito seller scope order", () => {
    const schema = runtimeConfigSchemaForOrigin("https://seller.example.com");
    const config = {
      cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
      cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
      cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
      client_id: "public-client",
      redirect_uri: "https://seller.example.com/auth/callback",
      scopes: ["openid", "mr-lister-api/seller"],
    };
    expect(schema.safeParse(config).success).toBe(true);
    expect(schema.safeParse({ ...config, scopes: [...config.scopes].reverse() }).success).toBe(false);
    expect(schema.safeParse({ ...config, scopes: ["openid"] }).success).toBe(false);
    expect(schema.safeParse({ ...config, cognito_token_url: "https://other.auth.us-west-2.amazoncognito.com/oauth2/token" }).success).toBe(false);
    expect(schema.safeParse({ ...config, cognito_authorize_url: "https://attacker.example/oauth2/authorize" }).success).toBe(false);
    expect(schema.safeParse({ ...config, cognito_logout_url: `${config.cognito_logout_url}?next=evil` }).success).toBe(false);
    expect(schema.safeParse({ ...config, redirect_uri: "https://other.example.com/auth/callback" }).success).toBe(false);
  });

  it("rejects cross-field presentation drift before rendering", () => {
    const pending = structuredClone(browserFixtures.seller_review_pending);
    expect(sellerReviewSchema.safeParse({
      ...pending,
      listing: { ...pending.listing, title: "Partial content" },
    }).success).toBe(false);
    expect(sellerReviewSchema.safeParse({
      ...pending,
      validation: { ...pending.validation, passed: true },
    }).success).toBe(false);
    expect(sellerReviewSchema.safeParse({
      ...pending,
      economics: { ...pending.economics, readiness: "ready" },
    }).success).toBe(false);
    const exactPreview = {
      ...pending,
      preview: {
        ...pending.preview,
        readiness: "ready",
        url: `${window.location.origin}/v1/jobs/${pending.job_id}/artwork-preview`,
        expires_at: "2026-08-22T12:05:00Z",
      },
    };
    expect(sellerReviewSchema.safeParse(exactPreview).success).toBe(true);
    expect(sellerReviewSchema.safeParse({
      ...exactPreview,
      preview: { ...exactPreview.preview, url: `${window.location.origin}/v1/jobs/other_job/artwork-preview` },
    }).success).toBe(false);
    expect(sellerReviewSchema.safeParse({
      ...exactPreview,
      preview: { ...exactPreview.preview, url: `https://attacker.example/v1/jobs/${pending.job_id}/artwork-preview` },
    }).success).toBe(false);
  });

  it("accepts only exact Printify mockup authorities", () => {
    const pending = structuredClone(browserFixtures.seller_review_pending);
    const withUrl = (url: string) => ({
      ...pending,
      mockups: {
        ...pending.mockups,
        readiness: "ready",
        items: [{ contract_version: "2.0.0", url, alt_text: "Front product mockup" }],
      },
    });
    expect(sellerReviewSchema.safeParse(withUrl("https://images.printify.com/product/front.png?quality=90")).success).toBe(true);
    for (const url of [
      "http://images.printify.com/product/front.png",
      "https://images.printify.com.evil.test/product/front.png",
      "https://images.printify.com:443/product/front.png",
      "https://images.printify.com/product/front.png#fragment",
      "https://images.printify.com/",
    ]) expect(sellerReviewSchema.safeParse(withUrl(url)).success).toBe(false);
  });

  it("rejects upload grants that cannot be safely forwarded to S3", () => {
    const digest = "a".repeat(64);
    const checksum = btoa(String.fromCharCode(...Array.from({ length: 32 }, () => 0xaa)));
    const authorization = {
      upload_id: "upload_1",
      job_id: "job_1",
      authorization_generation: 1,
      method: "POST",
      url: "https://private-bucket.s3.us-west-2.amazonaws.com/",
      form_fields: {
        key: `private/owners/${"b".repeat(64)}/jobs/job_1/source/source.png`,
        "Content-Type": "image/png",
        "x-amz-checksum-algorithm": "SHA256",
        "x-amz-checksum-sha256": checksum,
        "x-amz-server-side-encryption": "AES256",
        "x-amz-tagging": "mr-lister-state=staged",
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": "credential",
        "x-amz-date": "20260822T120000Z",
        policy: "policy",
        "x-amz-signature": "signature",
      },
      content_sha256: digest,
      size_bytes: 1024,
      issued_at: "2026-08-22T12:00:00Z",
      expires_at: "2026-08-22T12:05:00Z",
    };
    const response = { upload: { upload_id: "upload_1", job_id: "job_1", status: "open", record_version: 0 }, authorization };
    expect(uploadResponseSchema.safeParse(response).success).toBe(true);
    expect(uploadResponseSchema.safeParse({ ...response, authorization: { ...authorization, form_fields: { ...authorization.form_fields, unexpected: "forwarded" } } }).success).toBe(false);
    expect(uploadResponseSchema.safeParse({ ...response, authorization: { ...authorization, form_fields: { ...authorization.form_fields, "x-amz-checksum-sha256": "wrong" } } }).success).toBe(false);
    expect(uploadResponseSchema.safeParse({ ...response, authorization: { ...authorization, url: "https://private-bucket.s3.us-west-2.amazonaws.com.attacker.example/" } }).success).toBe(false);
    expect(uploadResponseSchema.safeParse({ ...response, upload: { ...response.upload, status: "completed" } }).success).toBe(false);
  });
});
