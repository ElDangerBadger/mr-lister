import { z } from "zod";
import type { RuntimeConfig } from "../contracts";

const TRANSACTION_KEY = "mr-lister.oauth-transaction.v1";
const SAFE_RESOURCE_RETURN = /^\/(?:jobs|uploads)\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/u;

const transactionSchema = z.strictObject({
  state: z.string().regex(/^[A-Za-z0-9_-]{32,256}$/u),
  verifier: z.string().regex(/^[A-Za-z0-9_-]{43,128}$/u),
  returnPath: z.string().max(256),
});

const tokenResponseSchema = z.strictObject({
  access_token: z.string().min(1).max(16_384),
  expires_in: z.number().int().positive().max(86_400),
  token_type: z.literal("Bearer"),
  id_token: z.string().min(1).max(16_384).optional(),
  refresh_token: z.string().min(1).max(16_384).optional(),
  scope: z.string().max(2_048).optional(),
});

export type SessionStatus = "anonymous" | "authenticated";

export interface AuthSession {
  getAccessToken(): string | null;
  getStatus(): SessionStatus;
  renewAccessToken(force?: boolean): Promise<string | null>;
  subscribe(listener: () => void): () => void;
  clear(): void;
}

export interface AuthCoordinator {
  readonly session: AuthSession;
  startSignIn(returnPath: string): Promise<void>;
  completeSignIn(callbackSearch: string): Promise<string>;
  signOut(): void;
}

export class MemoryAuthSession implements AuthSession {
  private accessToken: string | null = null;
  private expiresAt = 0;
  private refreshToken: string | null = null;
  private renewer: ((refreshToken: string) => Promise<{ accessToken: string; expiresInSeconds: number; refreshToken?: string }>) | null = null;
  private renewal: Promise<string | null> | null = null;
  private generation = 0;
  private readonly listeners = new Set<() => void>();

  getAccessToken(): string | null {
    if (this.accessToken !== null && Date.now() >= this.expiresAt) {
      this.accessToken = null;
      this.expiresAt = 0;
      this.emit();
    }
    return this.accessToken;
  }

  getStatus(): SessionStatus {
    this.getAccessToken();
    return this.accessToken === null && this.refreshToken === null ? "anonymous" : "authenticated";
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  set(accessToken: string, expiresInSeconds: number, refreshToken?: string): void {
    this.generation += 1;
    this.applyTokens(accessToken, expiresInSeconds, refreshToken);
  }

  private applyTokens(accessToken: string, expiresInSeconds: number, refreshToken?: string): void {
    this.accessToken = accessToken;
    this.expiresAt = Date.now() + Math.max(0, expiresInSeconds - 30) * 1_000;
    if (refreshToken !== undefined) this.refreshToken = refreshToken;
    this.emit();
  }

  setRenewer(renewer: (refreshToken: string) => Promise<{ accessToken: string; expiresInSeconds: number; refreshToken?: string }>): void {
    this.renewer = renewer;
  }

  async renewAccessToken(force = false): Promise<string | null> {
    if (force) {
      this.accessToken = null;
      this.expiresAt = 0;
    }
    if (this.getAccessToken() !== null) return this.accessToken;
    if (this.refreshToken === null || this.renewer === null) return null;
    if (this.renewal !== null) return this.renewal;
    const refreshToken = this.refreshToken;
    const generation = this.generation;
    this.renewal = this.renewer(refreshToken).then((tokens) => {
      if (generation !== this.generation) return null;
      this.applyTokens(tokens.accessToken, tokens.expiresInSeconds, tokens.refreshToken);
      return tokens.accessToken;
    }).catch(() => {
      if (generation === this.generation) this.clear();
      return null;
    }).finally(() => {
      this.renewal = null;
    });
    return this.renewal;
  }

  clear(): void {
    this.generation += 1;
    const changed = this.accessToken !== null || this.refreshToken !== null;
    this.accessToken = null;
    this.expiresAt = 0;
    this.refreshToken = null;
    if (changed) this.emit();
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}

export class OAuthCoordinator implements AuthCoordinator {
  readonly session: MemoryAuthSession;

  constructor(
    private readonly config: RuntimeConfig,
    session = new MemoryAuthSession(),
    private readonly storage: Storage = window.sessionStorage,
    private readonly fetcher: typeof fetch = window.fetch.bind(window),
    private readonly navigateTo: (target: URL) => void = (target) => window.location.assign(target),
  ) {
    this.session = session;
    this.session.setRenewer(async (refreshToken) => {
      const tokens = await this.exchangeTokens(new URLSearchParams({
        grant_type: "refresh_token",
        client_id: this.config.client_id,
        refresh_token: refreshToken,
      }));
      return {
        accessToken: tokens.access_token,
        expiresInSeconds: tokens.expires_in,
        ...(tokens.refresh_token === undefined ? {} : { refreshToken: tokens.refresh_token }),
      };
    });
  }

  async startSignIn(returnPath: string): Promise<void> {
    const transaction = await createPkceTransaction(returnPath);
    this.storage.setItem(TRANSACTION_KEY, JSON.stringify(transaction.stored));
    const target = new URL(this.config.cognito_authorize_url);
    target.searchParams.set("response_type", "code");
    target.searchParams.set("client_id", this.config.client_id);
    target.searchParams.set("redirect_uri", this.config.redirect_uri);
    target.searchParams.set("scope", this.config.scopes.join(" "));
    target.searchParams.set("state", transaction.stored.state);
    target.searchParams.set("code_challenge_method", "S256");
    target.searchParams.set("code_challenge", transaction.challenge);
    this.navigateTo(target);
  }

  async completeSignIn(callbackSearch: string): Promise<string> {
    const parameters = new URLSearchParams(callbackSearch);
    window.history.replaceState(null, "", "/auth/callback");
    try {
      if (parameters.getAll("error").length > 0) {
        throw new AuthError("Sign-in was not completed.");
      }
      const code = oneParameter(parameters, "code");
      const returnedState = oneParameter(parameters, "state");
      if (parameters.size !== 2 || [...parameters.keys()].some((key) => key !== "code" && key !== "state")) {
        throw new AuthError("The sign-in response contains unexpected data.");
      }
      const transaction = consumeTransaction(this.storage, returnedState);
      const body = new URLSearchParams({
        grant_type: "authorization_code",
        client_id: this.config.client_id,
        code,
        redirect_uri: this.config.redirect_uri,
        code_verifier: transaction.verifier,
      });
      const tokens = await this.exchangeTokens(body);
      this.session.set(tokens.access_token, tokens.expires_in, tokens.refresh_token);
      return transaction.returnPath;
    } catch (error) {
      this.storage.removeItem(TRANSACTION_KEY);
      throw error;
    }
  }

  signOut(): void {
    this.storage.removeItem(TRANSACTION_KEY);
    this.session.clear();
    const target = new URL(this.config.cognito_logout_url);
    target.searchParams.set("client_id", this.config.client_id);
    target.searchParams.set("logout_uri", new URL("/", this.config.redirect_uri).href);
    this.navigateTo(target);
  }

  private async exchangeTokens(body: URLSearchParams): Promise<z.infer<typeof tokenResponseSchema>> {
    const response = await this.fetcher(this.config.cognito_token_url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
    const text = await response.text();
    if (!response.ok || text.length > 65_536) {
      throw new AuthError("The sign-in session could not be established.");
    }
    let decoded: unknown;
    try {
      decoded = JSON.parse(text) as unknown;
    } catch {
      throw new AuthError("The identity service returned an invalid response.");
    }
    const tokens = tokenResponseSchema.safeParse(decoded);
    if (!tokens.success) {
      throw new AuthError("The identity service returned an invalid response.");
    }
    return tokens.data;
  }
}

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthError";
  }
}

export function validateReturnPath(candidate: string): string {
  if (candidate === "/") return candidate;
  const match = SAFE_RESOURCE_RETURN.exec(candidate);
  return match?.[0] === candidate && !candidate.includes("//") ? candidate : "/";
}

export async function createPkceTransaction(returnPath: string): Promise<{
  stored: z.infer<typeof transactionSchema>;
  challenge: string;
}> {
  const verifier = randomBase64Url(64);
  const state = randomBase64Url(32);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return {
    stored: { state, verifier, returnPath: validateReturnPath(returnPath) },
    challenge: encodeBase64Url(new Uint8Array(digest)),
  };
}

function consumeTransaction(storage: Storage, returnedState: string): z.infer<typeof transactionSchema> {
  const encoded = storage.getItem(TRANSACTION_KEY);
  storage.removeItem(TRANSACTION_KEY);
  if (encoded === null || encoded.length > 1_024) throw new AuthError("The sign-in session expired.");
  let candidate: unknown;
  try {
    candidate = JSON.parse(encoded) as unknown;
  } catch {
    throw new AuthError("The sign-in session is invalid.");
  }
  const parsed = transactionSchema.safeParse(candidate);
  if (!parsed.success || parsed.data.state !== returnedState) {
    throw new AuthError("The sign-in response could not be verified.");
  }
  return { ...parsed.data, returnPath: validateReturnPath(parsed.data.returnPath) };
}

function oneParameter(parameters: URLSearchParams, name: string): string {
  const values = parameters.getAll(name);
  if (values.length !== 1 || values[0] === undefined || values[0].length === 0 || values[0].length > 4_096) {
    throw new AuthError("The sign-in response is invalid.");
  }
  return values[0];
}

function randomBase64Url(byteLength: number): string {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  return encodeBase64Url(bytes);
}

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}
