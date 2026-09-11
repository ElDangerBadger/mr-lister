import { z } from "zod";
import type { RuntimeConfig } from "../contracts";

const TRANSACTION_KEY = "mr-lister.oauth-transaction.v1";
const POPUP_MARKER_KEY = "mr-lister.oauth-popup.v1";
const POPUP_NAME_PREFIX = "mr-lister-signin-";
const POPUP_CALLBACK_TYPE = "mr-lister.oauth-popup-callback.v1";
const POPUP_COMPLETE_TYPE = "mr-lister.oauth-popup-complete.v1";
const POPUP_TIMEOUT_MS = 10 * 60 * 1_000;
const MAX_CALLBACK_LENGTH = 16_384;
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

const popupCallbackSchema = z.strictObject({
  type: z.literal(POPUP_CALLBACK_TYPE),
  search: z.string().min(1).max(MAX_CALLBACK_LENGTH),
});

interface PopupAttempt {
  popup: Window;
  promise: Promise<string>;
  transaction: z.infer<typeof transactionSchema> | null;
  exchanging: boolean;
  resolve: (returnPath: string) => void;
  reject: (reason: AuthError) => void;
  listener: (event: MessageEvent<unknown>) => void;
  timeout: number;
  closedPoll: number;
}

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
  startPopupSignIn?(returnPath: string): Promise<string>;
  cancelPopupSignIn?(): void;
  focusPopupSignIn?(): void;
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
  private popupAttempt: PopupAttempt | null = null;
  private signInGeneration = 0;

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
    this.cancelPopupSignIn();
    const generation = this.signInGeneration;
    const transaction = await createPkceTransaction(returnPath);
    if (this.signInGeneration !== generation) throw new AuthError("Sign-in was canceled.");
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

  startPopupSignIn(returnPath: string): Promise<string> {
    if (this.popupAttempt !== null) {
      this.focusPopupSignIn();
      return this.popupAttempt.promise;
    }
    // Reserve the window during the click, before asynchronous PKCE hashing.
    const name = `${POPUP_NAME_PREFIX}${randomBase64Url(16)}`;
    const width = Math.min(520, window.screen.availWidth || 520);
    const height = Math.min(720, window.screen.availHeight || 720);
    const left = Math.max(0, window.screenX + (window.outerWidth - width) / 2);
    const top = Math.max(0, window.screenY + (window.outerHeight - height) / 2);
    let popup: Window | null;
    try {
      popup = window.open("", name, `popup=yes,width=${width},height=${height},left=${left},top=${top}`);
    } catch {
      return Promise.reject(new AuthError("The secure sign-in window could not be opened. You can continue in this tab."));
    }
    if (popup === null) {
      return Promise.reject(new AuthError("Your browser blocked the secure sign-in window. You can continue in this tab."));
    }
    try {
      // Only this non-secret ownership marker crosses navigation. PKCE stays in
      // the parent memory; clear any copied full-page transaction from the child.
      popup.sessionStorage.removeItem(TRANSACTION_KEY);
      popup.sessionStorage.setItem(POPUP_MARKER_KEY, name);
      this.storage.removeItem(TRANSACTION_KEY);
    } catch {
      popup.close();
      return Promise.reject(new AuthError("The secure sign-in window is unavailable. You can continue in this tab."));
    }
    this.signInGeneration += 1;
    const generation = this.signInGeneration;
    let resolveAttempt!: (returnPath: string) => void;
    let rejectAttempt!: (reason: AuthError) => void;
    const promise = new Promise<string>((resolve, reject) => {
      resolveAttempt = resolve;
      rejectAttempt = reject;
    });
    const attempt: PopupAttempt = {
      popup, promise, transaction: null, exchanging: false,
      resolve: resolveAttempt, reject: rejectAttempt,
      listener: (event) => { void this.receivePopupCallback(attempt, generation, event); },
      timeout: 0, closedPoll: 0,
    };
    this.popupAttempt = attempt;
    window.addEventListener("message", attempt.listener);
    attempt.timeout = window.setTimeout(() => {
      this.finishPopup(attempt, new AuthError("Sign-in timed out. Please try again."));
    }, POPUP_TIMEOUT_MS);
    attempt.closedPoll = window.setInterval(() => {
      if (!attempt.exchanging && attempt.popup.closed) {
        this.finishPopup(attempt, new AuthError("The sign-in window was closed. Please try again when you’re ready."));
      }
    }, 500);
    void createPkceTransaction(returnPath).then((transaction) => {
      if (this.popupAttempt !== attempt) return;
      attempt.transaction = transaction.stored;
      const target = new URL(this.config.cognito_authorize_url);
      target.searchParams.set("response_type", "code");
      target.searchParams.set("client_id", this.config.client_id);
      target.searchParams.set("redirect_uri", this.config.redirect_uri);
      target.searchParams.set("scope", this.config.scopes.join(" "));
      target.searchParams.set("state", transaction.stored.state);
      target.searchParams.set("code_challenge_method", "S256");
      target.searchParams.set("code_challenge", transaction.challenge);
      popup.location.replace(target.href);
      popup.focus();
    }).catch(() => {
      this.finishPopup(attempt, new AuthError("The secure sign-in window could not be prepared. Please try again."));
    });
    return promise;
  }

  cancelPopupSignIn(): void {
    this.signInGeneration += 1;
    if (this.popupAttempt !== null) {
      this.finishPopup(this.popupAttempt, new AuthError("Sign-in was canceled."));
    }
  }

  focusPopupSignIn(): void {
    try { this.popupAttempt?.popup.focus(); } catch { /* The window may have just closed. */ }
  }

  private async receivePopupCallback(attempt: PopupAttempt, generation: number, event: MessageEvent<unknown>): Promise<void> {
    if (this.popupAttempt !== attempt || attempt.exchanging || attempt.transaction === null
      || event.origin !== new URL(this.config.redirect_uri).origin || event.source !== attempt.popup) return;
    const message = popupCallbackSchema.safeParse(event.data);
    if (!message.success) return;
    const parameters = new URLSearchParams(message.data.search);
    const states = parameters.getAll("state");
    if (states.length !== 1 || states[0] !== attempt.transaction.state) return;
    const transaction = attempt.transaction;
    attempt.transaction = null;
    attempt.exchanging = true;
    try {
      const code = authorizationCode(parameters);
      attempt.popup.postMessage({ type: POPUP_COMPLETE_TYPE, state: transaction.state }, event.origin);
      const tokens = await this.exchangeAuthorizationCode(code, transaction.verifier);
      if (this.popupAttempt !== attempt || this.signInGeneration !== generation) return;
      this.session.set(tokens.access_token, tokens.expires_in, tokens.refresh_token);
      this.finishPopup(attempt, null, transaction.returnPath);
    } catch {
      this.finishPopup(attempt, new AuthError("Sign-in could not be completed. Please try again."));
    }
  }

  private finishPopup(attempt: PopupAttempt, error: AuthError | null, returnPath = "/"): void {
    if (this.popupAttempt !== attempt) return;
    this.popupAttempt = null;
    attempt.transaction = null;
    window.removeEventListener("message", attempt.listener);
    window.clearTimeout(attempt.timeout);
    window.clearInterval(attempt.closedPoll);
    try { attempt.popup.close(); } catch { /* Closing an already detached window is best effort. */ }
    if (error === null) attempt.resolve(returnPath);
    else attempt.reject(error);
  }

  async completeSignIn(callbackSearch: string): Promise<string> {
    const parameters = new URLSearchParams(callbackSearch);
    window.history.replaceState(null, "", "/auth/callback");
    const generation = this.signInGeneration;
    try {
      const code = authorizationCode(parameters);
      const returnedState = oneParameter(parameters, "state");
      const transaction = consumeTransaction(this.storage, returnedState);
      const tokens = await this.exchangeAuthorizationCode(code, transaction.verifier);
      if (this.signInGeneration !== generation) throw new AuthError("Sign-in was canceled.");
      this.session.set(tokens.access_token, tokens.expires_in, tokens.refresh_token);
      return transaction.returnPath;
    } catch (error) {
      this.storage.removeItem(TRANSACTION_KEY);
      throw error;
    }
  }

  signOut(): void {
    this.cancelPopupSignIn();
    this.storage.removeItem(TRANSACTION_KEY);
    this.session.clear();
    const target = new URL(this.config.cognito_logout_url);
    target.searchParams.set("client_id", this.config.client_id);
    target.searchParams.set("logout_uri", new URL("/", this.config.redirect_uri).href);
    this.navigateTo(target);
  }

  private exchangeAuthorizationCode(code: string, verifier: string): Promise<z.infer<typeof tokenResponseSchema>> {
    return this.exchangeTokens(new URLSearchParams({
      grant_type: "authorization_code", client_id: this.config.client_id, code,
      redirect_uri: this.config.redirect_uri, code_verifier: verifier,
    }));
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

/** Relay only an app-owned popup callback; exchange and session stay in its opener. */
export function relayPopupCallback(callbackSearch: string, onFailure?: (error: AuthError) => void): boolean {
  const ownedName = (value: string | null) => value !== null
    && /^mr-lister-signin-[A-Za-z0-9_-]{22}$/u.test(value);
  let marker: string | null = null;
  try { marker = window.sessionStorage.getItem(POPUP_MARKER_KEY); } catch { /* A valid window name still identifies the popup. */ }
  if (!ownedName(window.name) && !ownedName(marker)) return false;
  window.history.replaceState(null, "", "/auth/callback");
  window.name = "";
  try {
    window.sessionStorage.removeItem(POPUP_MARKER_KEY);
    window.sessionStorage.removeItem(TRANSACTION_KEY);
  } catch { /* No verifier or token is stored for this popup flow. */ }
  const opener: Window | null = window.opener as Window | null;
  if (opener === null || opener.closed) {
    throw new AuthError("The original sign-in page is no longer available. Please sign in again.");
  }
  if (callbackSearch.length === 0 || callbackSearch.length > MAX_CALLBACK_LENGTH) {
    throw new AuthError("The sign-in response is invalid. Please try again.");
  }
  const state = oneParameter(new URLSearchParams(callbackSearch), "state");
  const origin = window.location.origin;
  const onAcknowledged = (event: MessageEvent<unknown>) => {
    if (event.origin !== origin || event.source !== opener) return;
    const response = z.strictObject({ type: z.literal(POPUP_COMPLETE_TYPE), state: z.literal(state) }).safeParse(event.data);
    if (!response.success) return;
    window.removeEventListener("message", onAcknowledged);
    window.clearTimeout(timeout);
    window.close();
  };
  const timeout = window.setTimeout(() => {
    window.removeEventListener("message", onAcknowledged);
    onFailure?.(new AuthError("The original sign-in page did not respond. Please sign in again."));
  }, 10_000);
  window.addEventListener("message", onAcknowledged);
  try {
    opener.postMessage({ type: POPUP_CALLBACK_TYPE, search: callbackSearch }, origin);
  } catch {
    window.removeEventListener("message", onAcknowledged);
    window.clearTimeout(timeout);
    throw new AuthError("The sign-in response could not reach the original page. Please try again.");
  }
  return true;
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

function authorizationCode(parameters: URLSearchParams): string {
  if (parameters.getAll("error").length > 0) throw new AuthError("Sign-in was not completed.");
  const code = oneParameter(parameters, "code");
  if (parameters.size !== 2 || [...parameters.keys()].some((key) => key !== "code" && key !== "state")) {
    throw new AuthError("The sign-in response contains unexpected data.");
  }
  return code;
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
