import { afterEach, describe, expect, it, vi } from "vitest";
import { captureJudgeInvitation, JUDGE_RESTORE_BLOCK_KEY, JudgeSessionCoordinator } from "../src/auth/judge-session";
import { runtimeConfigSchemaForOrigin } from "../src/contracts";

const invitation = "a".repeat(43);
const token = (access = "judge-access-token", expires = 3600) => new Response(JSON.stringify({ access_token: access, expires_in: expires, token_type: "Bearer" }));
const capture = (value: string | null = invitation) => ({ invitation: value, fragmentPresent: value !== null });
function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); window.localStorage.removeItem(JUDGE_RESTORE_BLOCK_KEY); window.history.replaceState(null, "", "/"); });

describe("judge invitation capture", () => {
  it("removes the invitation before asynchronous work without persisting it", () => {
    window.history.replaceState({ keep: true }, "", `/judge/#access=${invitation}`);
    const value = captureJudgeInvitation();
    expect(value).toEqual(capture());
    expect(window.location.hash).toBe("");
    expect(window.history.state).toEqual({ keep: true });
    const storage = [window.localStorage, window.sessionStorage].flatMap((store) => Array.from({ length: store.length }, (_, index) => store.getItem(store.key(index)!))).join(" ");
    expect(storage).not.toContain(invitation);
  });
  it.each(["#access=short", `#access=${invitation}&next=https://foreign.example`, "#access=https://foreign.example/", `#access=${"a".repeat(257)}`, `#access=${invitation}%2F`, "#other=anything"])("rejects and removes malformed fragments: %s", (hash) => {
    window.history.replaceState(null, "", `/judge/jobs/job_example${hash}`);
    expect(captureJudgeInvitation()).toEqual({ invitation: null, fragmentPresent: true });
    expect(window.location.pathname).toBe("/judge/jobs/job_example");
    expect(window.location.hash).toBe("");
  });
  it("does not interpret fragments outside the judge workspace", () => {
    window.history.replaceState(null, "", `/#access=${invitation}`);
    expect(captureJudgeInvitation()).toEqual({ invitation: null, fragmentPresent: false });
    expect(window.location.hash).toContain(invitation);
  });
});

describe("JudgeSessionCoordinator", () => {
  it("requires an explicit entry action, then redeems only to the fixed same-origin endpoint", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(token());
    const captured = capture();
    const auth = new JudgeSessionCoordinator(captured, fetcher);
    await auth.restore();
    expect(fetcher).not.toHaveBeenCalled();
    expect(captured.invitation).toBeNull();
    await auth.startSignIn();
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/v1/judge-session/redeem", expect.objectContaining({
      method: "POST", body: JSON.stringify({ invitation }), credentials: "same-origin", cache: "no-store", redirect: "error",
    }));
    expect(auth.session.getAccessToken()).toBe("judge-access-token");
    expect(auth.session.getStatus()).toBe("authenticated");
  });
  it("restores at most once with a cookie and an empty JSON body", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(token());
    const auth = new JudgeSessionCoordinator(capture(null), fetcher);
    await Promise.all([auth.restore(), auth.restore()]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith("/v1/judge-session/refresh", expect.objectContaining({ body: "{}", credentials: "same-origin" }));
    expect(auth.session.getStatus()).toBe("authenticated");
  });
  it("does not refresh a malformed access link automatically", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const auth = new JudgeSessionCoordinator({ invitation: null, fragmentPresent: true }, fetcher);
    await auth.restore();
    expect(fetcher).not.toHaveBeenCalled();
    expect(auth.getEntryState().phase).toBe("missing");
  });
  it("canceled background restoration settles without accepting late tokens", async () => {
    const pending = deferred();
    const auth = new JudgeSessionCoordinator(capture(null), vi.fn<typeof fetch>().mockReturnValue(pending.promise));
    const restoration = auth.restore();
    auth.cancel();
    pending.resolve(token());
    await expect(restoration).resolves.toBeUndefined();
    expect(auth.session.getStatus()).toBe("anonymous");
    const reloadFetch = vi.fn<typeof fetch>();
    await new JudgeSessionCoordinator(capture(null), reloadFetch).restore();
    expect(reloadFetch).not.toHaveBeenCalled();
  });
  it("shows access-link instructions when no cookie is available", async () => {
    const auth = new JudgeSessionCoordinator(capture(null), vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 })));
    await auth.restore();
    expect(auth.session.getStatus()).toBe("anonymous");
    expect(auth.getEntryState().phase).toBe("missing");
    expect(auth.getEntryState().message).toContain("private judge access link");
  });
  it("deduplicates entry clicks and never accepts a canceled response", async () => {
    const pending = deferred();
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    const auth = new JudgeSessionCoordinator(capture(), fetcher);
    const entering = auth.startSignIn();
    expect(auth.startSignIn()).toBe(entering);
    const rejected = expect(entering).rejects.toThrow("canceled");
    auth.cancel();
    pending.resolve(token());
    await rejected;
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(auth.session.getStatus()).toBe("anonymous");
    expect(auth.getEntryState().phase).toBe("missing");
    expect(window.localStorage.getItem(JUDGE_RESTORE_BLOCK_KEY)).toBe("1");
    const reloadFetch = vi.fn<typeof fetch>();
    const reloaded = new JudgeSessionCoordinator(capture(null), reloadFetch);
    await reloaded.restore();
    await expect(reloaded.startSignIn()).rejects.toThrow("private judge access link");
    expect(reloadFetch).not.toHaveBeenCalled();
  });
  it("ignores an old attempt after a new entry has succeeded", async () => {
    const pending = deferred();
    const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(pending.promise).mockResolvedValueOnce(token("new-token"));
    const auth = new JudgeSessionCoordinator(capture(), fetcher);
    const old = auth.startSignIn();
    const rejected = expect(old).rejects.toThrow("canceled");
    auth.cancel();
    const fresh = new JudgeSessionCoordinator(capture(), fetcher);
    await fresh.startSignIn();
    pending.resolve(token("old-token"));
    await rejected;
    expect(fresh.session.getAccessToken()).toBe("new-token");
    expect(auth.session.getAccessToken()).toBeNull();
  });
  it("renews expiry using only the cookie, never the internal renewal sentinel", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(token("first", 60)).mockResolvedValueOnce(token("second"));
    const auth = new JudgeSessionCoordinator(capture(), fetcher);
    await auth.startSignIn();
    vi.advanceTimersByTime(31_000);
    expect(auth.session.getAccessToken()).toBeNull();
    expect(await auth.session.renewAccessToken()).toBe("second");
    expect(fetcher.mock.calls[1]).toEqual(["/v1/judge-session/refresh", expect.objectContaining({ body: "{}" })]);
    expect(JSON.stringify(fetcher.mock.calls)).not.toContain("judge-http-only-session");
  });
  it("expires a revoked cookie and keeps server error details out of UI", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(token()).mockResolvedValueOnce(new Response("secret-details", { status: 403 }));
    const auth = new JudgeSessionCoordinator(capture(), fetcher);
    await auth.startSignIn();
    expect(await auth.session.renewAccessToken(true)).toBeNull();
    expect(auth.session.getStatus()).toBe("anonymous");
    expect(auth.getEntryState().message).not.toContain("secret-details");
  });
  it("clears local access before cookie logout and ignores pending renewal", async () => {
    const pending = deferred();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(token()).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(new Response(null, { status: 204 }));
    const auth = new JudgeSessionCoordinator(capture(), fetcher);
    await auth.startSignIn();
    const renewal = auth.session.renewAccessToken(true);
    auth.signOut();
    expect(auth.session.getStatus()).toBe("anonymous");
    pending.resolve(token("late"));
    expect(await renewal).toBeNull();
    expect(fetcher.mock.calls[2]).toEqual(["/v1/judge-session/logout", expect.objectContaining({ body: "{}", credentials: "same-origin" })]);
    expect(fetcher.mock.calls.every(([url]) => typeof url === "string" && url.startsWith("/v1/judge-session/"))).toBe(true);
  });
  it("blocks entry until failed cookie sign-out is deliberately retried", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(token()).mockResolvedValueOnce(new Response(null, { status: 503 })).mockResolvedValueOnce(new Response(null, { status: 204 }));
    const auth = new JudgeSessionCoordinator(capture(), fetcher);
    await auth.startSignIn();
    auth.signOut();
    await Promise.resolve();
    expect(auth.getEntryState().phase).toBe("signout-error");
    await expect(auth.startSignIn()).rejects.toThrow("sign-out");
    auth.signOut();
    await Promise.resolve();
    expect(auth.getEntryState().phase).toBe("missing");
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(["/v1/judge-session/redeem", "/v1/judge-session/logout", "/v1/judge-session/logout"]);
    expect(window.localStorage.getItem(JUDGE_RESTORE_BLOCK_KEY)).toBe("1");
    const reloadFetch = vi.fn<typeof fetch>();
    await new JudgeSessionCoordinator(capture(null), reloadFetch).restore();
    expect(reloadFetch).not.toHaveBeenCalled();
  });
  it("clears the reload block only after a fresh explicit valid-link redemption succeeds", async () => {
    window.localStorage.setItem(JUDGE_RESTORE_BLOCK_KEY, "1");
    const pending = deferred();
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    const fresh = new JudgeSessionCoordinator(capture(), fetcher);
    await fresh.restore();
    expect(fetcher).not.toHaveBeenCalled();
    const entry = fresh.startSignIn();
    expect(window.localStorage.getItem(JUDGE_RESTORE_BLOCK_KEY)).toBe("1");
    pending.resolve(token());
    await entry;
    expect(window.localStorage.getItem(JUDGE_RESTORE_BLOCK_KEY)).toBeNull();
    const reloadFetch = vi.fn<typeof fetch>().mockResolvedValue(token());
    await new JudgeSessionCoordinator(capture(null), reloadFetch).restore();
    expect(reloadFetch).toHaveBeenCalledTimes(1);
  });
  it("retains the reload block when fresh-link redemption fails", async () => {
    window.localStorage.setItem(JUDGE_RESTORE_BLOCK_KEY, "1");
    const auth = new JudgeSessionCoordinator(capture(), vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 403 })));
    await expect(auth.startSignIn()).rejects.toThrow("expired");
    expect(window.localStorage.getItem(JUDGE_RESTORE_BLOCK_KEY)).toBe("1");
  });
  it("never automatically restores when persistent storage is unavailable", async () => {
    const setter = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
    const fetcher = vi.fn<typeof fetch>();
    const auth = new JudgeSessionCoordinator(capture(null), fetcher);
    await auth.restore();
    await expect(auth.startSignIn()).rejects.toThrow("private judge access link");
    expect(fetcher).not.toHaveBeenCalled();
    setter.mockRestore();
  });
  it.each([{ access_token: "token", expires_in: 3600, token_type: "Bearer", refresh_token: "not-allowed" }, { access_token: "token", expires_in: -1, token_type: "Bearer" }, { access_token: "token", expires_in: 3600, token_type: "Other" }])("rejects malformed authority without starting a session", async (body) => {
    const auth = new JudgeSessionCoordinator(capture(), vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(body))));
    await expect(auth.startSignIn()).rejects.toThrow("invalid response");
    expect(auth.session.getStatus()).toBe("anonymous");
  });
  it("keeps feature-disabled failures safe and retryable", async () => {
    const auth = new JudgeSessionCoordinator(capture(), vi.fn<typeof fetch>().mockResolvedValue(new Response("internal-secret", { status: 503 })));
    await expect(auth.startSignIn()).rejects.toThrow("temporarily unavailable");
    expect(auth.getEntryState().message).not.toContain("internal-secret");
  });
});

it("allows session_entry only as an explicit true flag in judge configuration", () => {
  const base = {
    cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
    cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
    cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
    client_id: "primaryclient", redirect_uri: `${window.location.origin}/judge/auth/callback`,
    scopes: ["openid", "mr-lister-api/seller"],
    judge_access: { identity_provider: "MrListerJudge", upstream_logout_url: "https://judge.auth.us-west-2.amazoncognito.com/logout", upstream_client_id: "judgeclient", session_entry: true },
  };
  expect(runtimeConfigSchemaForOrigin(window.location.origin, "/judge").safeParse(base).success).toBe(true);
  expect(runtimeConfigSchemaForOrigin(window.location.origin).safeParse(base).success).toBe(false);
  expect(runtimeConfigSchemaForOrigin(window.location.origin, "/judge").safeParse({ ...base, judge_access: { ...base.judge_access, session_entry: "true" } }).success).toBe(false);
});
