import { afterEach, describe, expect, it, vi } from "vitest";
import type { RuntimeConfig } from "../src/contracts";
import { AuthError, MemoryAuthSession, OAuthCoordinator, relayPopupCallback } from "../src/auth/session";

const config: RuntimeConfig = {
  cognito_authorize_url: "https://seller-login.example.com/oauth2/authorize",
  cognito_token_url: "https://seller-login.example.com/oauth2/token",
  cognito_logout_url: "https://seller-login.example.com/logout",
  client_id: "public-client",
  redirect_uri: `${window.location.origin}/auth/callback`,
  scopes: ["openid", "mr-lister-api/seller"],
};
const callbackType = "mr-lister.oauth-popup-callback.v1";
const completedType = "mr-lister.oauth-popup-complete.v1";
const markerKey = "mr-lister.oauth-popup.v1";

function tokenResponse() {
  return new Response(JSON.stringify({
    access_token: "popup-access-secret", refresh_token: "popup-refresh-secret",
    expires_in: 3600, token_type: "Bearer",
  }), { status: 200 });
}

function fakePopup() {
  const stored = new Map<string, string>();
  const popup = {
    closed: false,
    focus: vi.fn(),
    close: vi.fn(() => { popup.closed = true; }),
    postMessage: vi.fn(),
    location: { replace: vi.fn() },
    sessionStorage: {
      removeItem: (key: string) => { stored.delete(key); },
      setItem: (key: string, value: string) => { stored.set(key, value); },
    },
  };
  return { popup, stored, handle: popup as unknown as Window };
}

async function authorizedPopup(fetcher = vi.fn<typeof fetch>().mockResolvedValue(tokenResponse())) {
  const fake = fakePopup();
  const open = vi.spyOn(window, "open").mockReturnValue(fake.handle);
  const session = new MemoryAuthSession();
  const coordinator = new OAuthCoordinator(config, session, window.sessionStorage, fetcher, vi.fn());
  const pending = coordinator.startPopupSignIn("/jobs/job_popup");
  // Attach rejection handling while tests exercise cancellation and timeouts.
  const outcome = pending.catch((reason: unknown) => reason);
  expect(open).toHaveBeenCalledOnce();
  await vi.waitFor(() => expect(fake.popup.location.replace).toHaveBeenCalledOnce());
  const target = new URL(fake.popup.location.replace.mock.calls[0]![0] as string);
  const state = target.searchParams.get("state")!;
  const send = (data: unknown, origin = window.location.origin, source = fake.handle) => {
    window.dispatchEvent(new MessageEvent("message", { origin, source, data }));
  };
  return { ...fake, open, session, coordinator, pending, outcome, state, send, fetcher, target };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.name = "";
});

describe("popup sign-in", () => {
  it("opens during the click, keeps PKCE out of storage, and authenticates the original page without changing its URL", async () => {
    window.history.replaceState(null, "", "/jobs/job_popup");
    const auth = await authorizedPopup();
    expect(auth.target.searchParams.get("code_challenge_method")).toBe("S256");
    expect(window.sessionStorage.length).toBe(0);
    expect([...auth.stored.keys()]).toEqual([markerKey]);
    expect(JSON.stringify([...auth.stored.values()])).not.toContain(auth.state);
    auth.send({ type: callbackType, search: `?code=one-use&state=${auth.state}` });
    await expect(auth.pending).resolves.toBe("/jobs/job_popup");
    expect(auth.session.getAccessToken()).toBe("popup-access-secret");
    expect(window.location.pathname).toBe("/jobs/job_popup");
    expect(window.sessionStorage.length).toBe(0);
    expect(auth.popup.close).toHaveBeenCalledOnce();
    expect(auth.popup.postMessage).toHaveBeenCalledWith({ type: completedType, state: auth.state }, window.location.origin);
    const body = auth.fetcher.mock.calls[0]?.[1]?.body;
    expect(body).toBeInstanceOf(URLSearchParams);
    expect((body as URLSearchParams).get("code_verifier")).toMatch(/^[A-Za-z0-9_-]{43,128}$/u);
  });

  it("ignores wrong origins, sources, states and malformed envelopes, and consumes an accepted callback once", async () => {
    const auth = await authorizedPopup();
    const callback = { type: callbackType, search: `?code=one-use&state=${auth.state}` };
    auth.send(callback, "https://attacker.example");
    auth.send(callback, window.location.origin, fakePopup().handle);
    auth.send({ ...callback, search: "?code=one-use&state=wrong" });
    auth.send({ ...callback, token: "unexpected" });
    auth.send({ ...callback, search: "x".repeat(16_385) });
    expect(auth.fetcher).not.toHaveBeenCalled();
    auth.send(callback);
    auth.send(callback);
    await expect(auth.pending).resolves.toBe("/jobs/job_popup");
    auth.send(callback);
    expect(auth.fetcher).toHaveBeenCalledOnce();
  });

  it.each([
    "code=one&code=two",
    "code=one&unexpected=data",
    "error=access_denied&error_description=private-details",
  ])("rejects an invalid matching callback without exchanging: %s", async (parameters) => {
    const auth = await authorizedPopup();
    auth.send({ type: callbackType, search: `?${parameters}&state=${auth.state}` });
    expect(await auth.outcome).toEqual(new AuthError("Sign-in could not be completed. Please try again."));
    expect(auth.fetcher).not.toHaveBeenCalled();
    expect(auth.session.getStatus()).toBe("anonymous");
  });

  it("reports popup blocking without redirecting or storing a transaction", async () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    const navigate = vi.fn();
    const coordinator = new OAuthCoordinator(config, new MemoryAuthSession(), window.sessionStorage, vi.fn(), navigate);
    await expect(coordinator.startPopupSignIn("/")).rejects.toThrow("blocked");
    expect(window.sessionStorage.length).toBe(0);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("focuses the existing attempt instead of opening another popup", async () => {
    const auth = await authorizedPopup();
    expect(auth.coordinator.startPopupSignIn("/")).toBe(auth.pending);
    expect(auth.open).toHaveBeenCalledOnce();
    expect(auth.popup.focus).toHaveBeenCalledTimes(2);
    auth.coordinator.cancelPopupSignIn();
    expect(await auth.outcome).toBeInstanceOf(AuthError);
    auth.send({ type: callbackType, search: `?code=late&state=${auth.state}` });
    expect(auth.fetcher).not.toHaveBeenCalled();
  });

  it.each(["cancel", "signOut", "timeout"] as const)("cannot restore a session from a late exchange after %s", async (action) => {
    vi.useFakeTimers();
    let resolveExchange!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>((resolve) => { resolveExchange = resolve; }));
    const auth = await authorizedPopup(fetcher);
    auth.send({ type: callbackType, search: `?code=one-use&state=${auth.state}` });
    expect(fetcher).toHaveBeenCalledOnce();
    if (action === "cancel") auth.coordinator.cancelPopupSignIn();
    else if (action === "signOut") auth.coordinator.signOut();
    else await vi.advanceTimersByTimeAsync(10 * 60 * 1_000);
    expect(await auth.outcome).toBeInstanceOf(AuthError);
    resolveExchange(tokenResponse());
    await vi.waitFor(() => expect(auth.popup.close).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(0);
    expect(auth.session.getStatus()).toBe("anonymous");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("handles a closed popup and removes polling and callback listeners", async () => {
    vi.useFakeTimers();
    const auth = await authorizedPopup();
    auth.popup.closed = true;
    await vi.advanceTimersByTimeAsync(500);
    expect(await auth.outcome).toEqual(new AuthError("The sign-in window was closed. Please try again when you’re ready."));
    expect(vi.getTimerCount()).toBe(0);
    auth.send({ type: callbackType, search: `?code=late&state=${auth.state}` });
    expect(auth.fetcher).not.toHaveBeenCalled();
  });

  it("does not persist or navigate a full-page fallback canceled while PKCE hashing is pending", async () => {
    let resolveDigest!: (digest: ArrayBuffer) => void;
    vi.spyOn(crypto.subtle, "digest").mockReturnValue(new Promise<ArrayBuffer>((resolve) => { resolveDigest = resolve; }));
    const navigate = vi.fn();
    const coordinator = new OAuthCoordinator(config, new MemoryAuthSession(), window.sessionStorage, vi.fn(), navigate);
    const outcome = coordinator.startSignIn("/jobs/job_popup").catch((reason: unknown) => reason);
    coordinator.cancelPopupSignIn();
    resolveDigest(new ArrayBuffer(32));
    expect(await outcome).toEqual(new AuthError("Sign-in was canceled."));
    expect(window.sessionStorage.length).toBe(0);
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("popup callback relay", () => {
  it("leaves ordinary full-page callbacks for the existing flow", () => {
    window.history.replaceState(null, "", "/auth/callback?code=one-use&state=state");
    expect(relayPopupCallback(window.location.search)).toBe(false);
    expect(window.location.search).not.toBe("");
  });

  it("uses the ownership marker if navigation reset the name, scrubs the query and closes only for an exact acknowledgement", () => {
    vi.useFakeTimers();
    const opener = fakePopup();
    vi.stubGlobal("opener", opener.handle);
    const close = vi.spyOn(window, "close").mockImplementation(() => undefined);
    window.sessionStorage.setItem(markerKey, `mr-lister-signin-${"a".repeat(22)}`);
    window.history.replaceState(null, "", "/auth/callback?code=one-use&state=callback-state");
    expect(relayPopupCallback(window.location.search)).toBe(true);
    expect(window.location.search).toBe("");
    expect(window.sessionStorage.length).toBe(0);
    expect(opener.popup.postMessage).toHaveBeenCalledWith({
      type: callbackType, search: "?code=one-use&state=callback-state",
    }, window.location.origin);
    const data = { type: completedType, state: "callback-state" };
    window.dispatchEvent(new MessageEvent("message", { data, origin: "https://attacker.example", source: opener.handle }));
    window.dispatchEvent(new MessageEvent("message", { data, origin: window.location.origin, source: fakePopup().handle }));
    window.dispatchEvent(new MessageEvent("message", { data: { ...data, state: "wrong" }, origin: window.location.origin, source: opener.handle }));
    expect(close).not.toHaveBeenCalled();
    window.dispatchEvent(new MessageEvent("message", { data, origin: window.location.origin, source: opener.handle }));
    expect(close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("scrubs the callback and reports a lost opener without exchanging in the popup", () => {
    vi.stubGlobal("opener", null);
    window.name = `mr-lister-signin-${"a".repeat(22)}`;
    window.history.replaceState(null, "", "/auth/callback?code=one-use&state=state");
    expect(() => relayPopupCallback(window.location.search)).toThrow("original sign-in page");
    expect(window.location.search).toBe("");
    expect(window.name).toBe("");
  });

  it("reports a lost parent attempt instead of leaving the callback waiting forever", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("opener", fakePopup().handle);
    window.name = `mr-lister-signin-${"a".repeat(22)}`;
    const onFailure = vi.fn();
    expect(relayPopupCallback("?code=one-use&state=state", onFailure)).toBe(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(onFailure).toHaveBeenCalledWith(new AuthError("The original sign-in page did not respond. Please sign in again."));
    expect(vi.getTimerCount()).toBe(0);
  });
});
