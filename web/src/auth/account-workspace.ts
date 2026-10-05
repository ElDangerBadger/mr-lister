import { z } from "zod";
import type { AuthSession } from "./session";

/** Presentation hints only. The account API independently verifies identity and entitlement. */
export interface AccountWorkspaceConfiguration {
  clientId: string;
  cognitoTokenUrl: string;
  issuer?: string;
  selfServiceSignup: boolean;
  connectionMethod?: "unavailable" | "oauth" | "personal_token";
  connectedWorkflow?: boolean;
  supportEmail?: string;
  noticeVersion?: string;
}

const accountClaimsSchema = z.object({
  token_use: z.literal("access"),
  client_id: z.string(),
  iss: z.string(),
  sub: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u),
  "cognito:groups": z.array(z.string().max(256)).max(100),
});

export function accountIdentity(token: string, config: AccountWorkspaceConfiguration): string | null {
  const claims = accountClaims(token, config);
  return claims === null ? null : `${claims.iss}\0${claims.sub}`;
}

/** This hint can only keep the UI locked; API authorization remains server-side. */
export function hasAccountWorkflowToken(token: string, config: AccountWorkspaceConfiguration): boolean {
  return accountClaims(token, config)?.["cognito:groups"].includes("seller") === true;
}

function accountClaims(token: string, config: AccountWorkspaceConfiguration): z.infer<typeof accountClaimsSchema> | null {
  if (token.length > 16_384) return null;
  const parts = token.split(".");
  if (parts.length !== 3 || !parts[1] || !/^[A-Za-z0-9_-]+$/u.test(parts[1])) return null;
  try {
    const bytes = Uint8Array.from(atob(parts[1].replace(/-/gu, "+").replace(/_/gu, "/")), (character) => character.charCodeAt(0));
    const claims = accountClaimsSchema.safeParse(JSON.parse(new TextDecoder().decode(bytes)) as unknown);
    if (!claims.success || claims.data.client_id !== config.clientId || !claims.data["cognito:groups"].includes("account")) return null;
    const issuer = /^https:\/\/cognito-idp\.([a-z0-9-]+)\.amazonaws\.com\/([a-z0-9-]+_[A-Za-z0-9]+)$/u.exec(claims.data.iss);
    if (issuer === null || !new URL(config.cognitoTokenUrl).hostname.endsWith(`.auth.${issuer[1]}.amazoncognito.com`)
      || (config.issuer !== undefined && claims.data.iss !== config.issuer)) return null;
    return claims.data;
  } catch {
    return null;
  }
}

const storeSchema = z.strictObject({
  connection_id: z.string().regex(/^conn_[a-f0-9]{32}$/u),
  shop_binding_id: z.string().regex(/^binding_[a-f0-9]{32}$/u),
  shop_id: z.number().int().positive().safe(),
  name: z.string().trim().min(1).max(256).refine((value) => !/[\u0000-\u001f\u007f]/u.test(value)),
  sales_channel: z.literal("etsy"),
});

export const accountSetupSchema = z.strictObject({
  contract_version: z.literal("account-setup-v1"),
  record_version: z.number().int().positive().safe(),
  state: z.enum(["connection_unavailable", "connection_required", "choose_store", "ready", "reconnect_required"]),
  connection_method: z.enum(["unavailable", "oauth", "personal_token"]),
  store: storeSchema.nullable(),
}).superRefine((value, context) => {
  if ((value.connection_method === "unavailable" && value.state !== "connection_unavailable")
    || (value.state === "ready" && value.store === null)
    || (!["ready", "reconnect_required"].includes(value.state) && value.store !== null)) {
    context.addIssue({ code: "custom", message: "Account setup state is inconsistent" });
  }
});

export type AccountSetup = z.infer<typeof accountSetupSchema>;
export interface AccountSetupPort {
  getSetup(identity: string, signal: AbortSignal): Promise<AccountSetup>;
}

export class AccountSetupError extends Error {
  constructor(readonly code: "session_expired" | "unavailable") {
    super(code === "session_expired" ? "Sign in again to continue account setup." : "We couldn’t load your account setup. Please try again.");
    this.name = "AccountSetupError";
  }
}

export class BrowserAccountSetupClient implements AccountSetupPort {
  constructor(private readonly session: AuthSession, private readonly config: AccountWorkspaceConfiguration, private readonly fetcher: typeof fetch = window.fetch.bind(window)) {}

  async getSetup(identity: string, signal: AbortSignal): Promise<AccountSetup> {
    try {
      let token = this.session.getAccessToken() ?? await this.session.renewAccessToken();
      const requireIdentity = () => {
        if (signal.aborted || token === null || accountIdentity(token, this.config) !== identity
          || this.session.getAccessToken() !== token) throw new AccountSetupError("session_expired");
      };
      const perform = () => {
        requireIdentity();
        return this.fetcher(this.config.connectionMethod === "personal_token" ? "/v1/store-setup" : "/v1/account", {
          method: "GET", headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
          credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", signal,
        });
      };
      let response = await perform();
      if (response.status === 401) {
        requireIdentity();
        token = await this.session.renewAccessToken(true);
        response = await perform();
      }
      if (response.status === 401 || response.status === 403) throw new AccountSetupError("session_expired");
      if (!response.ok || response.headers.get("content-type")?.split(";")[0]?.trim() !== "application/json") throw new AccountSetupError("unavailable");
      const text = await boundedResponseText(response);
      if (signal.aborted || this.session.getStatus() !== "authenticated") throw new AccountSetupError("session_expired");
      const current = this.session.getAccessToken();
      if (current === null || accountIdentity(current, this.config) !== identity) throw new AccountSetupError("session_expired");
      return accountSetupSchema.parse(JSON.parse(text) as unknown);
    } catch (error) {
      throw error instanceof AccountSetupError ? error : new AccountSetupError("unavailable");
    }
  }
}

export async function boundedResponseText(response: Response, maximumBytes = 16_384): Promise<string> {
  const reader = response.body?.getReader();
  if (reader === undefined) throw new AccountSetupError("unavailable");
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let bytes = 0;
  let text = "";
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) return text + decoder.decode();
      bytes += chunk.value.byteLength;
      if (bytes > maximumBytes) throw new AccountSetupError("unavailable");
      text += decoder.decode(chunk.value, { stream: true });
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
