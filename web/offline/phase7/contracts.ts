import { z } from "zod";

const contractVersion = z.literal("7.0.1");
const publicId = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u);
const fingerprint = z.string().regex(/^[a-f0-9]{64}$/u);
const dateTime = z.string().datetime({ offset: true });

export const publicationStates = [
  "not_requested",
  "publication_requested",
  "publication_verifying",
  "publication_reconciling",
  "published",
  "publication_failed",
  "publication_outcome_unknown",
] as const;

export const publicationStages = [
  "awaiting_activation",
  "queued",
  "preflight",
  "publishing",
  "verifying",
  "reconciling",
  "complete",
] as const;

const canonicalEtsyListingUrl = z.string().regex(
  /^https:\/\/www\.etsy\.com\/listing\/[1-9][0-9]{0,12}$/u,
);

export const publicationRequestBodySchema = z.strictObject({
  expected_record_version: z.number().int().nonnegative(),
  expected_review_version: z.number().int().positive(),
  expected_review_fingerprint: fingerprint,
  confirmation: z.literal("publish_exact_approved_listing"),
});

export const publicationRequestResponseSchema = z.strictObject({
  contract_version: contractVersion,
  job_id: publicId,
  publication_aggregate_id: publicId,
  publication_state: z.literal("publication_requested"),
  record_version: z.number().int().positive(),
  review_version: z.number().int().positive(),
  work_request_id: publicId,
  requested_at: dateTime,
  verification_deadline: dateTime,
}).superRefine((value, context) => {
  const requestedAt = Date.parse(value.requested_at);
  const deadline = Date.parse(value.verification_deadline);
  if (deadline - requestedAt !== 30 * 60 * 1_000) {
    context.addIssue({ code: "custom", message: "Publication deadline is not fixed at 30 minutes" });
  }
});

export const sellerPublicationProjectionSchema = z.strictObject({
  contract_version: contractVersion,
  job_id: publicId,
  publication_enabled: z.literal(false),
  request_enabled: z.literal(false),
  request_disabled_reason: z.literal("PUBLICATION_NOT_ACTIVATED"),
  state: z.enum(publicationStates),
  stage: z.enum(publicationStages),
  aggregate_record_version: z.number().int().nonnegative().nullable(),
  attempt_status: z.enum(["open", "terminal"]).nullable(),
  verification_deadline: dateTime.nullable(),
  safe_listing_url: canonicalEtsyListingUrl.nullable(),
  verified_at: dateTime.nullable(),
  report_id: publicId.nullable(),
  terminal_at: dateTime.nullable(),
  notification_available: z.boolean(),
  updated_at: dateTime,
  etag: fingerprint,
}).superRefine((value, context) => {
  const requested = value.state !== "not_requested";
  const terminal = [
    "published",
    "publication_failed",
    "publication_outcome_unknown",
  ].includes(value.state);
  const published = value.state === "published";
  const exactStages: Record<(typeof publicationStates)[number], ReadonlySet<string>> = {
    not_requested: new Set(["awaiting_activation"]),
    publication_requested: new Set(["queued", "preflight", "publishing"]),
    publication_verifying: new Set(["verifying"]),
    publication_reconciling: new Set(["reconciling"]),
    published: new Set(["complete"]),
    publication_failed: new Set(["complete"]),
    publication_outcome_unknown: new Set(["complete"]),
  };
  if (!exactStages[value.state].has(value.stage)) {
    context.addIssue({ code: "custom", path: ["stage"], message: "Publication stage is incoherent" });
  }
  const hasAggregate = value.aggregate_record_version !== null
    && value.attempt_status !== null
    && value.verification_deadline !== null;
  if (requested !== hasAggregate) {
    context.addIssue({ code: "custom", message: "Publication aggregate authority is incomplete" });
  }
  if (!requested && (
    value.report_id !== null
    || value.terminal_at !== null
    || value.safe_listing_url !== null
    || value.verified_at !== null
    || value.notification_available
  )) {
    context.addIssue({ code: "custom", message: "Unrequested publication exposes result authority" });
  }
  if (requested && value.attempt_status !== (terminal ? "terminal" : "open")) {
    context.addIssue({ code: "custom", path: ["attempt_status"], message: "Attempt status is incoherent" });
  }
  const hasTerminalAuthority = value.report_id !== null && value.terminal_at !== null;
  if (terminal !== hasTerminalAuthority) {
    context.addIssue({ code: "custom", message: "Terminal publication authority is incomplete" });
  }
  if (terminal && value.terminal_at !== value.updated_at) {
    context.addIssue({ code: "custom", path: ["updated_at"], message: "Terminal time is incoherent" });
  }
  const hasVerifiedResult = value.safe_listing_url !== null
    && value.verified_at !== null
    && value.notification_available;
  if (published !== hasVerifiedResult) {
    context.addIssue({ code: "custom", message: "Verified publication result is incomplete" });
  }
  if (published && value.verification_deadline !== null && value.verified_at !== null
    && value.terminal_at !== null && (
      Date.parse(value.verified_at) >= Date.parse(value.verification_deadline)
      || Date.parse(value.terminal_at) < Date.parse(value.verified_at)
    )) {
    context.addIssue({ code: "custom", message: "Verified result is outside its deadline" });
  }
  if (value.state === "publication_outcome_unknown" && value.verification_deadline !== null
    && value.terminal_at !== null
    && Date.parse(value.terminal_at) < Date.parse(value.verification_deadline)) {
    context.addIssue({ code: "custom", message: "Unknown outcome settled before its deadline" });
  }
});

export const publicationErrorSchema = z.strictObject({
  error: z.strictObject({
    code: z.string().regex(/^[A-Z][A-Z0-9_]{0,99}$/u),
    message: z.string().min(1).max(300),
  }),
});

export type PublicationRequestBody = z.infer<typeof publicationRequestBodySchema>;
export type PublicationRequestResponse = z.infer<typeof publicationRequestResponseSchema>;
export type SellerPublicationProjection = z.infer<typeof sellerPublicationProjectionSchema>;
