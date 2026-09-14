import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthError, MemoryAuthSession, OAuthCoordinator, type SignInWorkspaceOptions } from "../src/auth/session";
import { isJudgeSignIn, judgeWorkspacePath, resolveJudgeWorkspace } from "../src/auth/workspace";
import type { RuntimeConfig } from "../src/contracts";
import { judgeSignOutTarget } from "../src/runtime";

const origin = window.location.origin;
const poolId = "us-west-2_ExamplePool";
const judgeGroup = `${poolId}_MrListerJudge`;
const seller: RuntimeConfig = {
  cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
  cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
  cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
  client_id: "primaryclient",
  redirect_uri: `${origin}/auth/callback`,
  scopes: ["openid", "mr-lister-api/seller"],
};
const judge: RuntimeConfig = {
  ...seller,
  redirect_uri: `${origin}/judge/auth/callback`,
  judge_access: {
    identity_provider: "MrListerJudge",
    upstream_logout_url: "https://judges.auth.us-west-2.amazoncognito.com/logout",
    upstream_client_id: "judgeclient",
    prepared_job_id: "job_prepared",
  },
};
const claims = {
  token_use: "access",
  client_id: seller.client_id,
  iss: `https://cognito-idp.us-west-2.amazonaws.com/${poolId}`,
  "cognito:groups": ["seller", judgeGroup],
};
const callbackType = "mr-lister.oauth-popup-callback.v1";

// These unsigned synthetic tokens exercise presentation hints, never authorization.
function token(value: unknown = claims): string {
  return `header.${btoa(JSON.stringify(value)).replace(/\+/gu, "-").replace(/\//gu, "_").replace(/=+$/u, "")}.signature`;
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}

function setup(options: {
  current?: RuntimeConfig;
  accessToken?: string;
  companion?: () => Promise<Response>;
  resolver?: SignInWorkspaceOptions["resolveWorkspace"];
} = {}) {
  const session = new MemoryAuthSession();
  const accessToken = options.accessToken ?? token();
  const tokenFetch = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(json({
    access_token: accessToken, refresh_token: "memory-only-refresh", expires_in: 3600, token_type: "Bearer",
  })));
  const runtimeFetch = vi.fn<typeof fetch>().mockImplementation(options.companion ?? (() => Promise.resolve(json(judge))));
  const resolve = vi.fn<SignInWorkspaceOptions["resolveWorkspace"]>(options.resolver
    ?? ((access, current) => resolveJudgeWorkspace(access, current, runtimeFetch)));
  const activate = vi.fn<SignInWorkspaceOptions["activateWorkspace"]>((_config, path) => {
    window.history.replaceState(null, "", judgeWorkspacePath(path));
  });
  const destinations: URL[] = [];
  const auth = new OAuthCoordinator(options.current ?? seller, session, window.sessionStorage, tokenFetch,
    (url) => { destinations.push(url); }, { resolveWorkspace: resolve, activateWorkspace: activate });
  return { auth, session, accessToken, tokenFetch, runtimeFetch, resolve, activate, destinations };
}

async function startPopup(value: ReturnType<typeof setup>, returnPath = "/") {
  const stored = new Map<string, string>();
  const popup = {
    closed: false,
    focus: vi.fn(),
    close: vi.fn(() => { popup.closed = true; }),
    postMessage: vi.fn(),
    location: { replace: vi.fn() },
    sessionStorage: {
      removeItem: (key: string) => { stored.delete(key); },
      setItem: (key: string, item: string) => { stored.set(key, item); },
    },
  };
  const handle = popup as unknown as Window;
  vi.spyOn(window, "open").mockReturnValue(handle);
  const pending = value.auth.startPopupSignIn(returnPath);
  const outcome = pending.catch((reason: unknown) => reason);
  await vi.waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce());
  const target = new URL(popup.location.replace.mock.calls[0]![0] as string);
  const send = () => window.dispatchEvent(new MessageEvent("message", {
    origin, source: handle,
    data: { type: callbackType, search: `?code=one-use&state=${target.searchParams.get("state")}` },
  }));
  return { popup, stored, pending, outcome, target, send };
}

async function fullPageCallback(value: ReturnType<typeof setup>, path = "/") {
  await value.auth.startSignIn(path);
  const target = value.destinations[0]!;
  const search = `?code=one-use&state=${target.searchParams.get("state")}`;
  window.history.replaceState(null, "", `${new URL(target.searchParams.get("redirect_uri")!).pathname}${search}`);
  return search;
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("judge workspace presentation hints", () => {
  it("recognizes the issuer's exact automatic judge group in an access token for this client", () => {
    expect(isJudgeSignIn(token(), seller)).toBe(true);
    expect(isJudgeSignIn(token({ ...claims, "cognito:groups": [judgeGroup] }), seller)).toBe(true);
  });

  it.each([
    ["native seller", { ...claims, "cognito:groups": ["seller"] }],
    ["missing groups", { ...claims, "cognito:groups": undefined }],
    ["group substring", { ...claims, "cognito:groups": [`prefix_${judgeGroup}`] }],
    ["group suffix", { ...claims, "cognito:groups": [`${judgeGroup}_other`] }],
    ["different pool group", { ...claims, "cognito:groups": ["us-west-2_OtherPool_MrListerJudge"] }],
    ["wrong group case", { ...claims, "cognito:groups": [judgeGroup.toLowerCase()] }],
    ["string groups", { ...claims, "cognito:groups": judgeGroup }],
    ["wrong client", { ...claims, client_id: "otherclient" }],
    ["ID token", { ...claims, token_use: "id" }],
    ["non-Cognito issuer", { ...claims, iss: `https://attacker.example/${poolId}` }],
    ["different region", { ...claims, iss: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OtherPool", "cognito:groups": ["us-east-1_OtherPool_MrListerJudge"] }],
    ["issuer query", { ...claims, iss: `${claims.iss}?next=other` }],
    ["non-object claims", [claims]],
  ])("does not select judge presentation from %s", (_label, value) => {
    expect(isJudgeSignIn(token(value), seller)).toBe(false);
  });

  it.each([
    ["empty", ""], ["opaque", "opaque-token"], ["bad encoding", "header.%.signature"],
    ["non-JSON", "header.bm90LWpzb24.signature"], ["four segments", "a.b.c.d"], ["oversized", "x".repeat(16_385)],
  ])(
    "handles %s token without throwing", (_label, value) => {
      expect(isJudgeSignIn(value, seller)).toBe(false);
    },
  );

  it.each([
    ["/", "/judge"], ["/jobs/job_123", "/judge/jobs/job_123"], ["/uploads/upload_123", "/judge/uploads/upload_123"],
    ["//attacker.example", "/judge"], ["https://attacker.example", "/judge"], ["/jobs/job_123/../other", "/judge"],
    ["/judge/jobs/job_123", "/judge"], ["/jobs/job_123?token=secret", "/judge"],
  ])("keeps return path %s within the canonical judge workspace", (input, expected) => {
    expect(judgeWorkspacePath(input)).toBe(expected);
  });
});

describe("judge companion runtime binding", () => {
  it("loads the strict judge config without changing the document or sending credentials", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(judge));
    await expect(resolveJudgeWorkspace(token(), seller, fetcher)).resolves.toEqual(judge);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/judge/runtime-config.json", {
      cache: "no-store", credentials: "omit", headers: { Accept: "application/json" }, redirect: "error",
    });
    expect(window.location.pathname).toBe("/");
    expect(window.sessionStorage.length).toBe(0);
  });

  it("does not fetch companion config for native sellers or an already active judge workspace", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(resolveJudgeWorkspace(token({ ...claims, "cognito:groups": ["seller"] }), seller, fetcher)).resolves.toBeNull();
    await expect(resolveJudgeWorkspace(token(), judge, fetcher)).resolves.toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    ["different client", { ...judge, client_id: "otherclient" }],
    ["different authority", {
      ...judge,
      cognito_authorize_url: seller.cognito_authorize_url.replace("seller.auth", "other.auth"),
      cognito_token_url: seller.cognito_token_url.replace("seller.auth", "other.auth"),
      cognito_logout_url: seller.cognito_logout_url.replace("seller.auth", "other.auth"),
    }],
    ["different scopes", { ...judge, scopes: ["openid", "other/scope"] }],
    ["normal callback", { ...judge, redirect_uri: seller.redirect_uri }],
    ["foreign callback origin", { ...judge, redirect_uri: "https://other.example/judge/auth/callback" }],
    ["untrusted upstream logout", { ...judge, judge_access: { ...judge.judge_access, upstream_logout_url: "https://other.example/logout" } }],
    ["secret in public config", { ...judge, client_secret: "must-not-be-accepted" }],
  ])("rejects a companion with %s", async (_label, candidate) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(candidate));
    await expect(resolveJudgeWorkspace(token(), seller, fetcher)).rejects.toThrow();
    expect(window.location.pathname).toBe("/");
  });
});

describe("judge selection inside a root-originated OAuth flow", () => {
  it("retires the popup before activation and keeps the original callback bound to its token exchange", async () => {
    const value = setup();
    const attempt = await startPopup(value, "/jobs/job_return");
    const observedPaths: string[] = [];
    value.session.subscribe(() => { observedPaths.push(window.location.pathname); });
    value.activate.mockImplementation((config, path) => {
      expect(config).toEqual(judge);
      expect(attempt.popup.closed).toBe(true);
      expect(value.session.getStatus()).toBe("anonymous");
      // Model cleanup of the old SignInProvider during its synchronous remount.
      value.auth.cancelPopupSignIn();
      window.history.replaceState(null, "", judgeWorkspacePath(path));
    });
    expect(attempt.target.searchParams.get("identity_provider")).toBe("COGNITO");
    expect(attempt.target.searchParams.get("redirect_uri")).toBe(seller.redirect_uri);
    attempt.send();
    await expect(attempt.pending).resolves.toBeNull();
    expect(value.tokenFetch).toHaveBeenCalledOnce();
    const body = value.tokenFetch.mock.calls[0]![1]!.body as URLSearchParams;
    expect(body.get("redirect_uri")).toBe(seller.redirect_uri);
    expect(body.get("client_id")).toBe(seller.client_id);
    expect(body.get("code_verifier")).toMatch(/^[A-Za-z0-9_-]{43,128}$/u);
    expect(value.activate).toHaveBeenCalledExactlyOnceWith(judge, "/jobs/job_return");
    expect(observedPaths).toEqual(["/judge/jobs/job_return"]);
    expect(value.session.getAccessToken()).toBe(value.accessToken);
    expect(window.sessionStorage.length).toBe(0);
    expect(JSON.stringify([...attempt.stored])).not.toContain(value.accessToken);
    expect(JSON.stringify([...attempt.stored])).not.toContain("memory-only-refresh");
    value.auth.signOut();
    expect(value.destinations.at(-1)?.searchParams.get("logout_uri")).toBe(`${origin}/judge/signout`);
    const upstream = judgeSignOutTarget(judge, "/judge/signout")!;
    expect(upstream.searchParams.get("client_id")).toBe("judgeclient");
    expect(upstream.searchParams.get("logout_uri")).toBe(`${origin}/judge/`);
  });

  it("switches a full-page root callback without a second code exchange or stored tokens", async () => {
    const value = setup();
    const search = await fullPageCallback(value, "/uploads/upload_return");
    await expect(value.auth.completeSignIn(search)).resolves.toBeNull();
    expect(value.tokenFetch).toHaveBeenCalledOnce();
    expect((value.tokenFetch.mock.calls[0]![1]!.body as URLSearchParams).get("redirect_uri")).toBe(seller.redirect_uri);
    expect(window.location.pathname).toBe("/judge/uploads/upload_return");
    expect(window.location.search).toBe("");
    expect(value.session.getAccessToken()).toBe(value.accessToken);
    expect(window.sessionStorage.length).toBe(0);
    expect(value.destinations).toHaveLength(1);
    value.auth.signOut();
    expect(value.destinations.at(-1)?.searchParams.get("logout_uri")).toBe(`${origin}/judge/signout`);
  });

  it.each(["seller", "direct judge"] as const)("preserves the existing %s flow", async (mode) => {
    const current = mode === "seller" ? seller : judge;
    const value = setup({ current, accessToken: token({ ...claims, "cognito:groups": mode === "seller" ? ["seller"] : [judgeGroup] }) });
    const search = await fullPageCallback(value, "/jobs/job_return");
    await expect(value.auth.completeSignIn(search)).resolves.toBe("/jobs/job_return");
    expect(value.activate).not.toHaveBeenCalled();
    expect(value.runtimeFetch).not.toHaveBeenCalled();
    expect(value.session.getStatus()).toBe("authenticated");
    expect(value.destinations[0]?.searchParams.get("identity_provider")).toBe(mode === "seller" ? "COGNITO" : "MrListerJudge");
    value.auth.signOut();
    expect(value.destinations.at(-1)?.searchParams.get("logout_uri")).toBe(`${origin}${mode === "seller" ? "/" : "/judge/signout"}`);
  });

  it("keeps the session anonymous until companion configuration finishes", async () => {
    const companion = deferred<Response>();
    const value = setup({ companion: () => companion.promise });
    const attempt = await startPopup(value);
    const changed = vi.fn();
    value.session.subscribe(changed);
    attempt.send();
    await vi.waitFor(() => expect(value.runtimeFetch).toHaveBeenCalledOnce());
    expect(value.session.getStatus()).toBe("anonymous");
    expect(changed).not.toHaveBeenCalled();
    expect(value.activate).not.toHaveBeenCalled();
    companion.resolve(json(judge));
    await expect(attempt.pending).resolves.toBeNull();
    expect(changed).toHaveBeenCalledOnce();
  });

  it.each(["cancel", "signOut", "timeout"] as const)("cannot accept a delayed resolver after popup %s", async (action) => {
    vi.useFakeTimers();
    const resolved = deferred<RuntimeConfig | null>();
    const value = setup({ resolver: () => resolved.promise });
    const attempt = await startPopup(value);
    attempt.send();
    await vi.waitFor(() => expect(value.resolve).toHaveBeenCalledOnce());
    if (action === "cancel") value.auth.cancelPopupSignIn();
    else if (action === "signOut") value.auth.signOut();
    else await vi.advanceTimersByTimeAsync(10 * 60 * 1_000);
    expect(await attempt.outcome).toBeInstanceOf(AuthError);
    resolved.resolve(judge);
    await vi.advanceTimersByTimeAsync(0);
    expect(value.activate).not.toHaveBeenCalled();
    expect(value.session.getStatus()).toBe("anonymous");
    expect(window.location.pathname).toBe("/");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cannot accept a full-page resolver after sign-out and scrubs its callback while waiting", async () => {
    const resolved = deferred<RuntimeConfig | null>();
    const value = setup({ resolver: () => resolved.promise });
    const search = await fullPageCallback(value);
    const outcome = value.auth.completeSignIn(search).catch((reason: unknown) => reason);
    await vi.waitFor(() => expect(value.resolve).toHaveBeenCalledOnce());
    expect(window.location.search).toBe("");
    expect(window.sessionStorage.length).toBe(0);
    value.auth.signOut();
    resolved.resolve(judge);
    expect(await outcome).toBeInstanceOf(AuthError);
    expect(value.activate).not.toHaveBeenCalled();
    expect(value.session.getStatus()).toBe("anonymous");
  });

  it.each(["unavailable", "wrong client"] as const)("fails anonymously when companion configuration is %s", async (failure) => {
    const value = setup({ companion: () => Promise.resolve(failure === "unavailable" ? json({}, 503) : json({ ...judge, client_id: "otherclient" })) });
    const attempt = await startPopup(value);
    attempt.send();
    expect(await attempt.outcome).toBeInstanceOf(AuthError);
    expect(value.activate).not.toHaveBeenCalled();
    expect(value.session.getStatus()).toBe("anonymous");
    expect(window.location.pathname).toBe("/");
    value.auth.signOut();
    expect(value.destinations.at(-1)?.searchParams.get("logout_uri")).toBe(`${origin}/`);
  });

  it("rejects a throwing activation after popup retirement without committing tokens or new logout config", async () => {
    const value = setup();
    value.activate.mockImplementation(() => { throw new Error("activation failed"); });
    const attempt = await startPopup(value);
    attempt.send();
    expect(await attempt.outcome).toBeInstanceOf(AuthError);
    expect(attempt.popup.close).toHaveBeenCalledOnce();
    expect(value.session.getStatus()).toBe("anonymous");
    value.auth.signOut();
    expect(value.destinations.at(-1)?.searchParams.get("logout_uri")).toBe(`${origin}/`);
  });
});
