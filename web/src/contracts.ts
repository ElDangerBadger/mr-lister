import { z } from "zod";

const contractVersion = z.literal("2.0.0");
const publicId = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u);
const fingerprint = z.string().regex(/^[a-f0-9]{64}$/u);
const publicCode = z.string().regex(/^[A-Z][A-Z0-9_]{0,99}$/u);
const publicText = z.string().min(1).max(300);
const dateTime = z.string().datetime({ offset: true });
const sectionReadiness = z.enum(["pending", "ready", "outdated", "unavailable"]);
const httpsUrl = z.string().url().refine((value) => {
  const parsed = new URL(value);
  return parsed.protocol === "https:" && parsed.username === "" && parsed.password === "";
}, "Expected a credential-free HTTPS URL");
const s3UploadUrl = httpsUrl.refine((value) => {
  const parsed = new URL(value);
  return value === `${parsed.origin}/`
    && parsed.port === ""
    && parsed.pathname === "/"
    && parsed.search === ""
    && parsed.hash === ""
    && /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*s3(?:\.[a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?$/u.test(parsed.hostname);
}, "Expected an AWS S3 upload URL");
const printifyMockupUrl = z.string().min(1).max(2_048).refine(isExactPrintifyMockupUrl, {
  message: "Expected an exact Printify mockup URL",
});

export const sellerActions = [
  "edit_listing",
  "approve_review",
  "cancel_job",
  "retry_job",
  "refresh_economics",
] as const;

export const displayStates = [
  "preparing",
  "needs_revision",
  "synchronizing",
  "ready_for_review",
  "refreshing_estimate",
  "reconciling",
  "cancelling",
  "retryable_failure",
  "terminal_failure",
  "cancelled",
  "approved",
] as const;

export const reviewStages = [
  "upload_verified",
  "artwork_review",
  "listing_validation",
  "seller_revision",
  "product_sync",
  "human_review",
  "economics_refresh",
  "provider_reconciliation",
  "cancellation",
  "recovery",
  "complete",
] as const;

export const actionSchema = z.strictObject({
  contract_version: contractVersion,
  action: z.enum(sellerActions),
  enabled: z.boolean(),
  reason: z.enum([
    "AVAILABLE",
    "NOT_IN_CURRENT_STATE",
    "REVIEW_NOT_READY",
    "REVIEW_INVALID",
    "PRODUCT_NOT_CURRENT",
    "PRODUCT_NOT_REVIEWABLE",
    "MOCKUPS_NOT_READY",
    "ECONOMICS_MISSING",
    "ECONOMICS_STALE",
    "PROVIDER_OUTCOME_UNCONFIRMED",
    "CANCELLATION_PENDING",
    "RETRY_NOT_AVAILABLE",
  ]),
  message: publicText,
}).superRefine((value, context) => {
  if (value.enabled !== (value.reason === "AVAILABLE")) {
    context.addIssue({ code: "custom", message: "Action capability is incoherent" });
  }
});

const previewSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  url: httpsUrl.max(2_048).nullable(),
  expires_at: dateTime.nullable(),
}).superRefine((value, context) => {
  const complete = value.url !== null && value.expires_at !== null;
  if ((value.readiness === "ready") !== complete) {
    context.addIssue({ code: "custom", message: "Preview authority is incomplete" });
  }
});

const artworkSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  subject: publicText.nullable(),
  visual_elements: z.array(publicText).max(20),
  styles: z.array(publicText).max(20),
  themes: z.array(publicText).max(20),
  visible_text: z.array(publicText).max(20),
  safety_notes: z.array(publicText).max(20),
  confidence: z.number().min(0).max(1).nullable(),
});

const listingSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  title: z.string().max(140).nullable(),
  description: z.string().max(100_000).nullable(),
  tags: z.array(z.string().max(20)).max(13),
  audience: z.array(publicText).max(20),
}).superRefine((value, context) => {
  const complete = value.title !== null && value.description !== null && value.tags.length === 13;
  const exposesPartialContent = value.title !== null
    || value.description !== null
    || value.tags.length > 0
    || value.audience.length > 0;
  if ((value.readiness === "ready" && !complete) || (value.readiness !== "ready" && exposesPartialContent)) {
    context.addIssue({ code: "custom", message: "Listing projection is incomplete" });
  }
});

const validationSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  passed: z.boolean().nullable(),
  issues: z.array(z.strictObject({
    contract_version: contractVersion,
    code: publicCode,
    path: z.string().min(1).max(100),
    severity: z.enum(["error", "warning"]),
    message: publicText,
  })).max(50),
}).superRefine((value, context) => {
  if ((value.readiness === "ready") !== (value.passed !== null)) {
    context.addIssue({ code: "custom", message: "Listing validation projection is incomplete" });
  }
});

const placementSchema = z.strictObject({
  contract_version: contractVersion,
  group_id: publicId,
  sizes: z.array(publicText).min(1).max(20),
  position: publicText,
  decoration_method: publicText,
  x: z.number().min(0).max(1),
  y: z.number().min(0).max(1),
  scale: z.number().gt(0).max(1),
  angle: z.number().int().min(-360).max(360),
});

const productPolicySchema = z.strictObject({
  contract_version: contractVersion,
  product_name: publicText,
  provider_name: publicText,
  colors: z.array(publicText).min(1).max(30),
  sizes: z.array(publicText).min(1).max(30),
  placements: z.array(placementSchema).min(1).max(10),
  retail_price_cents: z.number().int().positive(),
  buyer_shipping_cents: z.number().int().nonnegative(),
  currency: z.literal("USD"),
});

const synchronizationSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  product_id: publicId.nullable(),
  synchronized_at: dateTime.nullable(),
  review_version: z.number().int().positive().nullable(),
  editable_draft: z.boolean().nullable(),
});

const mockupsSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  items: z.array(z.strictObject({
    contract_version: contractVersion,
    url: printifyMockupUrl,
    alt_text: publicText,
  })).max(5),
}).superRefine((value, context) => {
  if ((value.readiness === "ready") !== (value.items.length > 0)) {
    context.addIssue({ code: "custom", message: "Mockup projection is incomplete" });
  }
});

const variantEconomicsSchema = z.strictObject({
  contract_version: contractVersion,
  color: publicText,
  size: publicText,
  retail_price_cents: z.number().int().positive(),
  buyer_shipping_cents: z.number().int().nonnegative(),
  production_cost_cents: z.number().int().nonnegative(),
  production_shipping_cents: z.number().int().nonnegative(),
  marketplace_fees_cents: z.number().int().nonnegative(),
  estimated_proceeds_cents: z.number().int(),
});

const economicsSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: z.enum(["missing", "refreshing", "ready", "stale", "outdated", "unavailable"]),
  currency: z.literal("USD"),
  label: z.literal("Estimated proceeds"),
  minimum_cents: z.number().int().nullable(),
  maximum_cents: z.number().int().nullable(),
  variants: z.array(variantEconomicsSchema).max(100),
  calculated_at: dateTime.nullable(),
  fresh_until: dateTime.nullable(),
  production_cost_source: z.literal("Connected production product readback").nullable(),
  production_cost_observed_at: dateTime.nullable(),
  production_shipping_source: z.literal("Connected production standard US shipping").nullable(),
  production_shipping_observed_at: dateTime.nullable(),
  fee_policy_source: z.literal("Etsy US standard fee policy").nullable(),
  fee_policy_id: z.literal("etsy-us-standard-v1").nullable(),
  fee_policy_verified_on: z.string().date().nullable(),
  assumptions: z.array(publicText).max(20),
}).superRefine((value, context) => {
  const displayable = value.readiness === "ready" || value.readiness === "stale";
  const complete = value.minimum_cents !== null
    && value.maximum_cents !== null
    && value.variants.length > 0
    && value.calculated_at !== null
    && value.fresh_until !== null
    && value.production_cost_source !== null
    && value.production_cost_observed_at !== null
    && value.production_shipping_source !== null
    && value.production_shipping_observed_at !== null
    && value.fee_policy_source !== null
    && value.fee_policy_id !== null
    && value.fee_policy_verified_on !== null;
  if (displayable !== complete) {
    context.addIssue({ code: "custom", message: "Economics evidence is incomplete" });
  }
});

const strandsSchema = z.strictObject({
  contract_version: contractVersion,
  readiness: sectionReadiness,
  framework: z.literal("strands-agents").nullable(),
  agent_id: z.literal("mr-lister-preparation").nullable(),
  prepared_review_version: z.number().int().positive().nullable(),
  correlation_id: z.string().regex(/^[a-f0-9]{24}$/u).nullable(),
  tool_calls: z.array(z.literal("record_prepared_review")).max(1),
  completed_at: dateTime.nullable(),
});

export const sellerReviewSchema = z.strictObject({
  contract_version: contractVersion,
  job_id: publicId,
  record_version: z.number().int().nonnegative(),
  review_version: z.number().int().nonnegative(),
  review_fingerprint: fingerprint.nullable(),
  review_authority_etag: fingerprint.nullable(),
  display_state: z.enum(displayStates),
  stage: z.enum(reviewStages),
  authority_notice: z.literal("Unpublished — not on Etsy"),
  actions: z.array(actionSchema).length(5),
  preview: previewSchema,
  artwork: artworkSchema,
  listing: listingSchema,
  validation: validationSchema,
  product_policy: productPolicySchema,
  synchronization: synchronizationSchema,
  mockups: mockupsSchema,
  economics: economicsSchema,
  strands: strandsSchema,
  failure: z.strictObject({
    contract_version: contractVersion,
    code: publicCode,
    message: publicText,
    stage: z.enum(reviewStages),
    retryable: z.boolean(),
    recovery: z.enum(sellerActions).nullable(),
  }).nullable(),
  provider_outcome_unconfirmed: z.boolean(),
  created_at: dateTime,
  updated_at: dateTime,
}).superRefine((value, context) => {
  if (value.actions.some((item, index) => item.action !== sellerActions[index])) {
    context.addIssue({ code: "custom", message: "Action surface is not closed" });
  }
  const hasReviewAuthority = value.review_version > 0;
  if (hasReviewAuthority !== (value.review_fingerprint !== null && value.review_authority_etag !== null)) {
    context.addIssue({ code: "custom", message: "Review authority is incomplete" });
  }
  if (value.preview.url !== null && !isPreviewForJob(value.preview.url, value.job_id)) {
    context.addIssue({ code: "custom", path: ["preview", "url"], message: "Artwork preview authority is inconsistent" });
  }
});

export const jobSummarySchema = z.strictObject({
  job_id: publicId,
  state: z.enum([
    "intake_validated", "analyzing_artwork", "listing_drafted", "needs_revision",
    "product_draft_syncing", "awaiting_approval", "pricing_refreshing",
    "reconciliation_required", "failed_retryable", "failed_terminal",
    "cancel_requested", "cancelled", "approved",
  ]),
  record_version: z.number().int().nonnegative(),
  review_version: z.number().int().nonnegative(),
  created_at: dateTime,
  updated_at: dateTime,
});

export const jobPageSchema = z.strictObject({
  jobs: z.array(jobSummarySchema).max(100),
  next_cursor: z.string().regex(/^[A-Za-z0-9_-]{1,200}$/u).nullable(),
});

export const jobProgressSchema = z.strictObject({
  contract_version: contractVersion,
  job_id: publicId,
  record_version: z.number().int().nonnegative(),
  review_version: z.number().int().nonnegative(),
  display_state: z.enum(displayStates),
  stage: z.enum(reviewStages),
  authority_notice: z.literal("Unpublished — not on Etsy"),
  actions: z.array(actionSchema).length(5),
  failure: z.strictObject({
    contract_version: contractVersion,
    code: publicCode,
    message: publicText,
    stage: z.enum(reviewStages),
    retryable: z.boolean(),
    recovery: z.enum(sellerActions).nullable(),
  }).nullable(),
  provider_outcome_unconfirmed: z.boolean(),
  created_at: dateTime,
  updated_at: dateTime,
}).superRefine((value, context) => {
  if (value.actions.some((item, index) => item.action !== sellerActions[index])) {
    context.addIssue({ code: "custom", message: "Action surface is not closed" });
  }
});

const uploadAuthorizationSchema = z.strictObject({
  upload_id: publicId,
  job_id: publicId,
  authorization_generation: z.number().int().positive(),
  method: z.literal("POST"),
  url: s3UploadUrl.max(2_048),
  form_fields: z.record(z.string().min(1).max(128), z.string().min(1).max(16_384)).refine(
    (fields) => Object.keys(fields).length >= 1 && Object.keys(fields).length <= 64,
    "Upload form field count is outside its bound",
  ),
  content_sha256: fingerprint,
  size_bytes: z.number().int().positive().max(5 * 1024 * 1024),
  issued_at: dateTime,
  expires_at: dateTime,
}).superRefine((value, context) => {
  const issuedAt = Date.parse(value.issued_at);
  const expiresAt = Date.parse(value.expires_at);
  if (!(issuedAt < expiresAt && expiresAt <= issuedAt + 5 * 60 * 1_000)) {
    context.addIssue({ code: "custom", message: "Upload authorization lifetime is invalid" });
  }
  const expectedFields: Record<string, string> = {
    "Content-Type": "image/png",
    "x-amz-checksum-algorithm": "SHA256",
    "x-amz-checksum-sha256": hexToBase64(value.content_sha256),
    "x-amz-server-side-encryption": "AES256",
    "x-amz-tagging": "mr-lister-state=staged",
  };
  const signingFields = ["x-amz-algorithm", "x-amz-credential", "x-amz-date", "policy", "x-amz-signature"];
  const allowedFields = new Set(["key", ...Object.keys(expectedFields), ...signingFields, "x-amz-security-token"]);
  if (Object.entries(expectedFields).some(([name, expected]) => value.form_fields[name] !== expected)) {
    context.addIssue({ code: "custom", path: ["form_fields"], message: "Upload form does not bind the required object fields" });
  }
  if (signingFields.some((name) => value.form_fields[name] === undefined)
    || Object.keys(value.form_fields).some((name) => !allowedFields.has(name))) {
    context.addIssue({ code: "custom", path: ["form_fields"], message: "Upload form signing fields are invalid" });
  }
  const escapedJobId = value.job_id.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  if (!new RegExp(`^private/owners/[a-f0-9]{64}/jobs/${escapedJobId}/source/source\\.png$`, "u").test(value.form_fields.key ?? "")) {
    context.addIssue({ code: "custom", path: ["form_fields", "key"], message: "Upload form object key is inconsistent" });
  }
});

export const uploadResponseSchema = z.strictObject({
  upload: z.strictObject({
    upload_id: publicId,
    job_id: publicId,
    status: z.enum(["open", "completed", "cancelled", "expired"]),
    record_version: z.number().int().nonnegative(),
  }),
  authorization: uploadAuthorizationSchema.nullable(),
}).superRefine((value, context) => {
  if (value.authorization !== null && (
    value.authorization.upload_id !== value.upload.upload_id
    || value.authorization.job_id !== value.upload.job_id
  )) {
    context.addIssue({ code: "custom", message: "Upload authorization authority is inconsistent" });
  }
  if (value.authorization !== null && value.upload.status !== "open") {
    context.addIssue({ code: "custom", message: "Only an open upload can carry authorization" });
  }
});

export const uploadRecoverySchema = z.strictObject({
  upload_id: publicId,
  job_id: publicId,
  status: z.enum(["open", "completed", "cancelled", "expired"]),
  filename: z.string().min(1).max(255),
  content_type: z.literal("image/png"),
  size_bytes: z.number().int().positive().max(5 * 1024 * 1024),
  record_version: z.number().int().nonnegative(),
  authorization_expires_at: dateTime.nullable(),
  intent_expires_at: dateTime,
  completed_at: dateTime.nullable(),
  cancelled_at: dateTime.nullable(),
  expired_at: dateTime.nullable(),
  created_at: dateTime,
  updated_at: dateTime,
});

export const commandResponseSchema = z.strictObject({
  job_id: publicId,
  state: jobSummarySchema.shape.state,
  record_version: z.number().int().nonnegative(),
  review_version: z.number().int().nonnegative(),
});

export const errorEnvelopeSchema = z.strictObject({
  error: z.strictObject({
    code: publicCode,
    message: publicText,
    request_id: z.string().min(1).max(128),
    fields: z.array(z.strictObject({
      path: z.string().regex(/^\$(?:\.[a-z_][a-z0-9_]{0,63}|\[[0-9]{1,3}\])*$/u).max(160),
      code: z.enum(["REQUIRED", "UNEXPECTED_FIELD", "INVALID_TYPE", "INVALID_LENGTH", "INVALID_FORMAT", "OUT_OF_RANGE", "INVALID_VALUE"]),
      message: z.string().min(1).max(100),
    })).max(25).nullable().optional(),
  }),
});

const runtimeConfigBaseSchema = z.strictObject({
  cognito_authorize_url: httpsUrl,
  cognito_token_url: httpsUrl,
  cognito_logout_url: httpsUrl,
  client_id: z.string().min(1).max(256),
  redirect_uri: z.string().url(),
  scopes: z.tuple([z.literal("openid"), z.literal("mr-lister-api/seller")]),
});

export function runtimeConfigSchemaForOrigin(applicationOrigin: string) {
  return runtimeConfigBaseSchema.superRefine((value, context) => {
    const endpoints = [
      ["cognito_authorize_url", value.cognito_authorize_url, "/oauth2/authorize"],
      ["cognito_token_url", value.cognito_token_url, "/oauth2/token"],
      ["cognito_logout_url", value.cognito_logout_url, "/logout"],
    ] as const;
    const parsedEndpoints = endpoints.map(([, url]) => new URL(url));
    const cognitoOrigin = parsedEndpoints[0]?.origin;
    const cognitoHostname = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.auth\.[a-z0-9-]+\.amazoncognito\.com$/u;
    endpoints.forEach(([field, , expectedPath], index) => {
      const parsed = parsedEndpoints[index];
      if (parsed === undefined
        || value[field] !== `${parsed.origin}${expectedPath}`
        || !cognitoHostname.test(parsed.hostname)
        || parsed.origin !== cognitoOrigin
        || parsed.port !== ""
        || parsed.pathname !== expectedPath
        || parsed.search !== ""
        || parsed.hash !== "") {
        context.addIssue({ code: "custom", path: [field], message: "Cognito endpoint authority is invalid" });
      }
    });
    const redirect = new URL(value.redirect_uri);
    if (redirect.origin !== new URL(applicationOrigin).origin
      || redirect.pathname !== "/auth/callback"
      || redirect.search !== ""
      || redirect.hash !== ""
      || redirect.username !== ""
      || redirect.password !== "") {
      context.addIssue({ code: "custom", path: ["redirect_uri"], message: "OAuth redirect does not match this application" });
    }
  });
}

export const runtimeConfigSchema = runtimeConfigSchemaForOrigin(window.location.origin);

export type SellerReview = z.infer<typeof sellerReviewSchema>;
export type SellerAction = (typeof sellerActions)[number];
export type JobSummary = z.infer<typeof jobSummarySchema>;
export type JobPage = z.infer<typeof jobPageSchema>;
export type JobProgress = z.infer<typeof jobProgressSchema>;
export type UploadResponse = z.infer<typeof uploadResponseSchema>;
export type UploadRecovery = z.infer<typeof uploadRecoverySchema>;
export type CommandResponse = z.infer<typeof commandResponseSchema>;
export type RuntimeConfig = z.infer<typeof runtimeConfigSchema>;

function isExactPrintifyMockupUrl(value: string): boolean {
  if (!value.startsWith("https://")
    || !/^[\x20-\x7e]+$/u.test(value)
    || value.includes("\\")
    || /%(?![0-9A-Fa-f]{2})/u.test(value)) return false;
  try {
    const parsed = new URL(value);
    const authority = value.slice("https://".length).split(/[/?#]/u, 1)[0];
    return parsed.protocol === "https:"
      && authority === "images.printify.com"
      && parsed.host === "images.printify.com"
      && parsed.hostname === "images.printify.com"
      && parsed.port === ""
      && parsed.username === ""
      && parsed.password === ""
      && parsed.pathname !== "/"
      && parsed.hash === "";
  } catch {
    return false;
  }
}

function isPreviewForJob(value: string, jobId: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.origin === window.location.origin
      && parsed.pathname === `/v1/jobs/${jobId}/artwork-preview`
      && parsed.search === ""
      && parsed.hash === "";
  } catch {
    return false;
  }
}

function hexToBase64(value: string): string {
  const bytes = value.match(/.{2}/gu)?.map((pair) => Number.parseInt(pair, 16)) ?? [];
  return btoa(String.fromCharCode(...bytes));
}
