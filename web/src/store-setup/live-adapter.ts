import { z } from "zod";
import { newIdempotencyKey } from "../api/client";
import { accountIdentity, accountSetupSchema, boundedResponseText, hasAccountWorkflowToken, type AccountSetup, type AccountWorkspaceConfiguration } from "../auth/account-workspace";
import type { AuthSession } from "../auth/session";
import { StoreConnectionError, type ConnectedStore, type StoreChoice, type StoreConnectionAdapter, type ValidatedConnection } from "./connection-adapter";
import { SERVICE_NOTICE_VERSION, areServiceNoticesReviewed, type MerchantAuthorization } from "../service-notices";

const safeText = (maximum: number) => z.string().trim().min(1).max(maximum).refine((value) => !/[\u0000-\u001f\u007f]/u.test(value));
const storeSchema = z.strictObject({
  id: z.string().regex(/^[1-9][0-9]{0,15}$/u).refine((value) => Number.isSafeInteger(Number(value))),
  name: safeText(256), sales_channel: safeText(64), eligible: z.boolean(),
  disabled_reason: z.enum(["unsupported_channel", "disconnected"]).nullable(),
}).superRefine((value, context) => {
  if (value.eligible ? value.sales_channel !== "etsy" || value.disabled_reason !== null : value.disabled_reason === null) context.addIssue({ code: "custom", message: "Store eligibility is inconsistent" });
});
const candidateSchema = z.strictObject({
  candidate_id: z.string().regex(/^candidate_[a-f0-9]{32}$/u),
  record_version: z.number().int().positive().safe(),
  expires_at: z.string().datetime(),
  stores: z.array(storeSchema).max(100),
}).superRefine((value, context) => {
  if (new Set(value.stores.map((store) => store.id)).size !== value.stores.length) context.addIssue({ code: "custom", message: "Store identifiers must be unique" });
});
const errorSchema = z.strictObject({ error: z.strictObject({ code: z.string().max(64), message: z.string().max(1024), request_id: z.string().max(256) }) });
const authorizationSchema = z.strictObject({ accepted: z.literal(true), terms_version: z.literal(SERVICE_NOTICE_VERSION), privacy_version: z.literal(SERVICE_NOTICE_VERSION) });
type Candidate = { value: z.infer<typeof candidateSchema>; identity: string; expires: number; selection: { shopId: string; key: string } | null };

/** Provider credentials cross only one authenticated request; candidates stay in memory. */
export class LiveStoreConnectionAdapter implements StoreConnectionAdapter {
  readonly mode = "live" as const;
  private candidate: Candidate | null = null;
  private generation = 0;
  private expiryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly session: AuthSession, private readonly config: AccountWorkspaceConfiguration, private readonly fetcher: typeof fetch = window.fetch.bind(window), private readonly now: () => number = Date.now) {}

  reset(): void {
    this.generation += 1;
    this.candidate = null;
    if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
    this.expiryTimer = null;
  }

  async validate(token: string, authorization: MerchantAuthorization, signal: AbortSignal): Promise<ValidatedConnection> {
    this.reset();
    const consent = authorizationSchema.safeParse(authorization);
    if (!consent.success) throw new StoreConnectionError("authorization_required");
    const generation = this.generation;
    const identity = await this.identity(signal);
    if (token.length < 1 || token.length > 4096 || /\s/u.test(token)) throw new StoreConnectionError("invalid_credentials");
    const candidate = candidateSchema.safeParse(await this.request("/v1/connections/printify/validate", { token, authorization: consent.data }, newIdempotencyKey("connect-printify"), identity, signal));
    token = "";
    if (!candidate.success || generation !== this.generation || signal.aborted) throw new StoreConnectionError("connection_failed");
    const expires = Date.parse(candidate.data.expires_at);
    if (!Number.isFinite(expires) || expires <= this.now() || expires > this.now() + 901_000) throw new StoreConnectionError("validation_expired");
    this.candidate = { value: candidate.data, identity, expires, selection: null };
    this.expiryTimer = setTimeout(() => { this.reset(); }, expires - this.now());
    return { validationId: candidate.data.candidate_id };
  }

  async listStores(validationId: string, signal: AbortSignal): Promise<readonly StoreChoice[]> {
    const candidate = await this.requireCandidate(validationId, signal);
    return candidate.value.stores.map((store) => ({ id: store.id, name: store.name, salesChannel: store.sales_channel, eligible: store.eligible, disabledReason: store.disabled_reason }));
  }

  async connect(validationId: string, storeId: string, signal: AbortSignal): Promise<ConnectedStore> {
    const candidate = await this.requireCandidate(validationId, signal);
    const selected = candidate.value.stores.find((store) => store.id === storeId);
    if (selected === undefined || !selected.eligible || selected.sales_channel !== "etsy") throw new StoreConnectionError("connection_failed");
    if (candidate.selection !== null && candidate.selection.shopId !== storeId) { this.reset(); throw new StoreConnectionError("validation_expired"); }
    candidate.selection ??= { shopId: storeId, key: newIdempotencyKey("select-printify-shop") };
    const generation = this.generation;
    const result = accountSetupSchema.safeParse(await this.request("/v1/connections/printify/select-shop", { candidate_id: validationId, shop_id: Number(storeId), expected_setup_version: candidate.value.record_version }, candidate.selection.key, candidate.identity, signal));
    if (generation !== this.generation || signal.aborted) throw new StoreConnectionError("validation_expired");
    if (!result.success) throw new StoreConnectionError("connection_failed");
    if (result.data.state !== "ready" || result.data.store === null) { this.reset(); throw new StoreConnectionError("setup_unavailable"); }
    if (result.data.connection_method !== "personal_token" || result.data.store.shop_id !== Number(storeId) || result.data.record_version <= candidate.value.record_version) throw new StoreConnectionError("connection_failed");
    const connected = await this.finish(result.data, candidate.identity, signal);
    if (generation !== this.generation) throw new StoreConnectionError("validation_expired");
    this.reset();
    return connected;
  }

  async activate(signal: AbortSignal): Promise<ConnectedStore> {
    this.reset();
    const generation = this.generation;
    const identity = await this.identity(signal);
    const result = accountSetupSchema.safeParse(await this.request("/v1/connections/printify/activate", {}, newIdempotencyKey("activate-printify"), identity, signal));
    if (generation !== this.generation || signal.aborted) throw new StoreConnectionError("session_expired");
    if (!result.success) throw new StoreConnectionError("connection_failed");
    const connected = await this.finish(result.data, identity, signal);
    if (generation !== this.generation) throw new StoreConnectionError("session_expired");
    return connected;
  }

  private async finish(setup: AccountSetup, identity: string, signal: AbortSignal): Promise<ConnectedStore> {
    if (setup.state !== "ready" || setup.connection_method !== "personal_token" || setup.store === null || this.config.connectedWorkflow !== true || !areServiceNoticesReviewed(this.config.noticeVersion, this.config.supportEmail)) throw new StoreConnectionError("setup_unavailable");
    const token = await this.session.renewAccessToken(true);
    if (signal.aborted || token === null || this.session.getAccessToken() !== token || accountIdentity(token, this.config) !== identity || !hasAccountWorkflowToken(token, this.config)) { this.reset(); throw new StoreConnectionError("session_expired"); }
    return { connectionId: setup.store.connection_id, store: { id: String(setup.store.shop_id), name: setup.store.name, salesChannel: "etsy", eligible: true, disabledReason: null }, setup };
  }

  private async requireCandidate(id: string, signal: AbortSignal): Promise<Candidate> {
    const identity = await this.identity(signal);
    const candidate = this.candidate;
    if (candidate === null || candidate.value.candidate_id !== id || candidate.expires <= this.now() || candidate.identity !== identity) {
      this.reset(); throw new StoreConnectionError("validation_expired");
    }
    return candidate;
  }

  private async identity(signal: AbortSignal): Promise<string> {
    const token = this.session.getAccessToken() ?? await this.session.renewAccessToken();
    const identity = token === null ? null : accountIdentity(token, this.config);
    if (signal.aborted || identity === null || this.session.getAccessToken() !== token || this.config.connectionMethod !== "personal_token") { this.reset(); throw new StoreConnectionError("session_expired"); }
    return identity;
  }

  private async request(path: string, body: Record<string, unknown>, idempotencyKey: string, identity: string, signal: AbortSignal): Promise<unknown> {
    try {
      const perform = async () => {
        if (await this.identity(signal) !== identity) throw new StoreConnectionError("session_expired");
        const token = this.session.getAccessToken();
        if (token === null || accountIdentity(token, this.config) !== identity) throw new StoreConnectionError("session_expired");
        return this.fetcher(path, { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", Authorization: `Bearer ${token}`, "Idempotency-Key": idempotencyKey }, body: JSON.stringify(body), signal, cache: "no-store", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer" });
      };
      let response = await perform();
      if (response.status === 401) {
        if (await this.identity(signal) !== identity) throw new StoreConnectionError("session_expired");
        await this.session.renewAccessToken(true);
        response = await perform();
      }
      if (response.status === 401 || response.status === 403) throw new StoreConnectionError("session_expired");
      if (response.headers.get("content-type")?.split(";")[0]?.trim() !== "application/json") throw new StoreConnectionError("connection_failed");
      const text = await boundedResponseText(response, 65_536);
      if (await this.identity(signal) !== identity) throw new StoreConnectionError("session_expired");
      const value: unknown = JSON.parse(text);
      if (!response.ok) {
        const parsed = errorSchema.safeParse(value);
        const code = parsed.success ? parsed.data.error.code : "";
        if (code === "CANDIDATE_EXPIRED" || code === "CONNECTION_CONFLICT") { this.reset(); throw new StoreConnectionError("validation_expired"); }
        if (code === "CREDENTIAL_NOT_ACCEPTED") throw new StoreConnectionError("invalid_credentials");
        throw new StoreConnectionError("connection_failed");
      }
      return value;
    } catch (error) {
      if (signal.aborted || error instanceof StoreConnectionError && error.code === "session_expired") this.reset();
      throw error instanceof StoreConnectionError ? error : new StoreConnectionError("connection_failed");
    }
  }
}
