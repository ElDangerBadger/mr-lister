import { z } from "zod";
import { AuthError, MemoryAuthSession, type AuthCoordinator } from "./session";

const COOKIE_RENEWAL = "judge-http-only-session";
export const JUDGE_RESTORE_BLOCK_KEY = "mr-lister.judge-restore-blocked.v1";
const ACCESS_LINK_MESSAGE = "Open the private judge access link provided to you to enter this workspace.";
class UnavailableJudgeAccess extends AuthError {}

function restorationStorage(): Storage | null {
  try {
    const storage = window.localStorage;
    // A readable but unwritable store cannot remember cancellation across reloads.
    const probe = `${JUDGE_RESTORE_BLOCK_KEY}.storage-check`;
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return storage;
  } catch {
    return null;
  }
}
const tokenSchema = z.strictObject({
  access_token: z.string().min(1).max(16_384),
  expires_in: z.number().int().positive().max(86_400),
  token_type: z.literal("Bearer"),
});

export interface JudgeInvitation {
  invitation: string | null;
  fragmentPresent: boolean;
}

/** Capture before any asynchronous bootstrap work. Fragments never choose a host or route. */
export function captureJudgeInvitation(): JudgeInvitation {
  if (window.location.pathname !== "/judge" && !window.location.pathname.startsWith("/judge/")) {
    return { invitation: null, fragmentPresent: false };
  }
  const fragment = window.location.hash;
  if (fragment === "") return { invitation: null, fragmentPresent: false };
  window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
  const match = /^#access=([A-Za-z0-9_-]{43,256})$/u.exec(fragment);
  return { invitation: match?.[1] ?? null, fragmentPresent: true };
}

export interface JudgeEntryState {
  phase: "ready" | "missing" | "restoring" | "entering" | "authenticated" | "error" | "signing-out" | "signout-error";
  message: string;
}

/** Judge access tokens live only in memory; renewal uses a same-origin HttpOnly cookie. */
export class JudgeSessionCoordinator implements AuthCoordinator {
  readonly session = new MemoryAuthSession();
  private invitation: string | null;
  private readonly mayRestore: boolean;
  private state: JudgeEntryState;
  private readonly listeners = new Set<() => void>();
  private generation = 0;
  private active: AbortController | null = null;
  private operation: Promise<void> | null = null;
  private restored = false;
  private restoreBlocked = false;

  constructor(
    captured: JudgeInvitation,
    private readonly fetcher: typeof fetch = window.fetch.bind(window),
    private readonly storage: Storage | null = restorationStorage(),
  ) {
    this.invitation = captured.invitation;
    captured.invitation = null;
    this.mayRestore = !captured.fragmentPresent;
    this.state = this.invitation === null
      ? { phase: "missing", message: ACCESS_LINK_MESSAGE }
      : { phase: "ready", message: "Your private link is ready. Enter when you’re ready to try the listing journey." };
    this.session.setRenewer(async () => {
      const generation = this.generation;
      const controller = new AbortController();
      this.active = controller;
      try {
        if (this.isRestoreBlocked()) throw new UnavailableJudgeAccess(ACCESS_LINK_MESSAGE);
        const tokens = await this.requestTokens("refresh", {}, controller.signal);
        if (generation !== this.generation) throw new AuthError("Judge access was canceled.");
        return { accessToken: tokens.access_token, expiresInSeconds: tokens.expires_in, refreshToken: COOKIE_RENEWAL };
      } catch {
        if (generation === this.generation) this.update("missing", "Your judge session has ended. Reopen your private access link to continue.");
        throw new AuthError("Your judge session has ended.");
      } finally {
        if (this.active === controller) this.active = null;
      }
    });
  }

  getEntryState = (): JudgeEntryState => this.state;
  subscribeEntry = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  /** A link preview with an invitation performs no redemption or cookie refresh. */
  restore(): Promise<void> {
    if (this.restored || !this.mayRestore || this.isRestoreBlocked()) return Promise.resolve();
    this.restored = true;
    return this.enter(true).catch(() => { /* A canceled background restore must stay anonymous. */ });
  }

  startSignIn(): Promise<void> {
    return this.enter(false);
  }

  completeSignIn(): Promise<null> {
    return Promise.reject(new AuthError("Open your private judge access link to enter this workspace."));
  }

  cancel(): void {
    this.blockRestoration();
    this.generation += 1;
    this.active?.abort();
    this.active = null;
    this.operation = null;
    this.invitation = null;
    this.update("missing", "Entry canceled. Reopen your private judge access link when you’re ready.");
  }

  signOut(): void {
    this.cancel();
    this.invitation = null;
    this.session.clear();
    const generation = this.generation;
    this.update("signing-out", "Ending your judge session…");
    void this.fetcher("/v1/judge-session/logout", {
      method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
      headers: { "Content-Type": "application/json", Accept: "application/json" }, body: "{}",
    }).then((response) => {
      if (generation !== this.generation) return;
      this.update(response.ok ? "missing" : "signout-error", response.ok ? "You’re signed out. Reopen your private access link to enter again." : "Your local session ended, but sign-out could not be confirmed. Try signing out again before leaving this browser.");
    }).catch(() => {
      if (generation === this.generation) this.update("signout-error", "Your local session ended, but sign-out could not be confirmed. Try signing out again before leaving this browser.");
    });
  }

  private enter(restoring: boolean): Promise<void> {
    if (this.operation !== null) return this.operation;
    if (this.state.phase === "signing-out" || this.state.phase === "signout-error") return Promise.reject(new AuthError("Wait for sign-out to finish."));
    if (this.session.getStatus() === "authenticated") return Promise.resolve();
    if (this.invitation === null && this.isRestoreBlocked()) return Promise.reject(new AuthError(ACCESS_LINK_MESSAGE));
    const generation = ++this.generation;
    const controller = new AbortController();
    this.active = controller;
    this.update(restoring ? "restoring" : "entering", restoring ? "Restoring your judge session…" : "Opening your judge workspace…");
    const invitation = this.invitation;
    const operation = this.requestTokens(invitation === null ? "refresh" : "redeem", invitation === null ? {} : { invitation }, controller.signal).then((tokens) => {
      if (generation !== this.generation) throw new AuthError("Judge access was canceled.");
      this.invitation = null;
      // Only an explicit, successful fresh-link redemption lifts a prior block.
      if (invitation !== null) this.clearRestorationBlock();
      this.update("authenticated", "Your judge workspace is ready.");
      this.session.set(tokens.access_token, tokens.expires_in, COOKIE_RENEWAL);
    }).catch((reason: unknown) => {
      if (generation !== this.generation) throw new AuthError("Judge access was canceled.");
      const message = reason instanceof AuthError ? reason.message : "Judge access is temporarily unavailable. Please try again.";
      if (reason instanceof UnavailableJudgeAccess) this.invitation = null;
      this.update(reason instanceof UnavailableJudgeAccess ? "missing" : "error", message);
      if (!restoring) throw new AuthError(message);
    }).finally(() => {
      if (this.operation === operation) this.operation = null;
      if (this.active === controller) this.active = null;
    });
    this.operation = operation;
    return operation;
  }

  private async requestTokens(action: "redeem" | "refresh", body: Record<string, string>, signal: AbortSignal) {
    const response = await this.fetcher(`/v1/judge-session/${action}`, {
      method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error", signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body),
    });
    if (response.status === 401 || response.status === 403) throw new UnavailableJudgeAccess(action === "redeem"
      ? "This judge access link has expired or is unavailable. Request a new private link."
      : ACCESS_LINK_MESSAGE);
    if (!response.ok) throw new AuthError("Judge access is temporarily unavailable. Please try again.");
    const text = await response.text();
    if (text.length > 32_768) throw new AuthError("Judge access returned an invalid response. Please try again.");
    const parsed = tokenSchema.safeParse(JSON.parse(text) as unknown);
    if (!parsed.success) throw new AuthError("Judge access returned an invalid response. Please try again.");
    return parsed.data;
  }

  private update(phase: JudgeEntryState["phase"], message: string): void {
    this.state = { phase, message };
    for (const listener of this.listeners) listener();
  }

  private isRestoreBlocked(): boolean {
    if (this.restoreBlocked || this.storage === null) return true;
    try {
      return this.storage.getItem(JUDGE_RESTORE_BLOCK_KEY) !== null;
    } catch {
      return true;
    }
  }

  private blockRestoration(): void {
    // Browser restore intent only; server-side revocation remains the logout endpoint's job.
    this.restoreBlocked = true;
    try { this.storage?.setItem(JUDGE_RESTORE_BLOCK_KEY, "1"); } catch { /* Remain blocked in memory. */ }
  }

  private clearRestorationBlock(): void {
    if (this.storage === null) return;
    try {
      this.storage.removeItem(JUDGE_RESTORE_BLOCK_KEY);
      this.restoreBlocked = this.storage.getItem(JUDGE_RESTORE_BLOCK_KEY) !== null;
    } catch {
      this.restoreBlocked = true;
    }
  }
}
