import { describe, expect, it, vi } from "vitest";
import type { RuntimeConfig } from "../src/contracts";
import { MemoryAuthSession, OAuthCoordinator, createPkceTransaction, validateReturnPath } from "../src/auth/session";

const config: RuntimeConfig = {
  cognito_authorize_url: "https://seller-login.example.com/oauth2/authorize",
  cognito_token_url: "https://seller-login.example.com/oauth2/token",
  cognito_logout_url: "https://seller-login.example.com/logout",
  client_id: "public-client",
  redirect_uri: "https://seller.example.com/auth/callback",
  scopes: ["openid", "mr-lister-api/seller"],
};

describe("OAuth public-client session", () => {
  it("accepts only exact local return paths", () => {
    expect(validateReturnPath("/jobs/job_123")).toBe("/jobs/job_123");
    expect(validateReturnPath("/uploads/upload-1")).toBe("/uploads/upload-1");
    expect(validateReturnPath("/jobs/job_123/../../admin")).toBe("/");
    expect(validateReturnPath("/jobs/job_123\nattack")).toBe("/");
    expect(validateReturnPath("//attacker.example")).toBe("/");
    expect(validateReturnPath("https://attacker.example")).toBe("/");
  });

  it("creates an S256 transaction with only state, verifier, and return path", async () => {
    const transaction = await createPkceTransaction("/jobs/job_123");
    expect(Object.keys(transaction.stored).sort()).toEqual(["returnPath", "state", "verifier"]);
    expect(transaction.stored.verifier.length).toBeGreaterThanOrEqual(43);
    expect(transaction.challenge).toMatch(/^[A-Za-z0-9_-]{43}$/u);
  });

  it("scrubs the callback, consumes one-use state, and keeps tokens out of storage", async () => {
    const target: URL[] = [];
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      access_token: "access-secret",
      refresh_token: "refresh-secret",
      expires_in: 3600,
      token_type: "Bearer",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const coordinator = new OAuthCoordinator(config, new MemoryAuthSession(), window.sessionStorage, fetcher, (url) => target.push(url));
    await coordinator.startSignIn("/jobs/job_123");
    expect(target[0]?.searchParams.get("code_challenge_method")).toBe("S256");
    expect(window.sessionStorage.length).toBe(1);
    const stored = JSON.parse(window.sessionStorage.getItem("mr-lister.oauth-transaction.v1") ?? "null") as { state: string };
    window.history.replaceState(null, "", `/auth/callback?code=one-use&state=${stored.state}`);
    const returnPath = await coordinator.completeSignIn(window.location.search);
    expect(returnPath).toBe("/jobs/job_123");
    expect(window.location.search).toBe("");
    expect(window.sessionStorage.length).toBe(0);
    expect(JSON.stringify(window.sessionStorage)).not.toContain("secret");
    expect(coordinator.session.getStatus()).toBe("authenticated");
    expect(fetcher.mock.calls[0]?.[1]?.redirect).toBe("error");
  });

  it("renews an expired access token once from an in-memory refresh token", async () => {
    const session = new MemoryAuthSession();
    const renewal = vi.fn().mockResolvedValue({ accessToken: "renewed", expiresInSeconds: 3600 });
    session.setRenewer(renewal);
    session.set("expired", 1, "memory-refresh");
    await expect(session.renewAccessToken()).resolves.toBe("renewed");
    expect(renewal).toHaveBeenCalledWith("memory-refresh");
    expect(session.getAccessToken()).toBe("renewed");
  });

  it("clears memory and redirects through the configured Cognito logout endpoint", () => {
    const targets: URL[] = [];
    const session = new MemoryAuthSession();
    session.set("access", 3600, "refresh");
    const coordinator = new OAuthCoordinator(config, session, window.sessionStorage, vi.fn(), (url) => targets.push(url));
    coordinator.signOut();
    expect(session.getStatus()).toBe("anonymous");
    expect(targets[0]?.origin).toBe("https://seller-login.example.com");
    expect(targets[0]?.searchParams.get("logout_uri")).toBe("https://seller.example.com/");
  });

  it("cannot resurrect a signed-out session from an in-flight renewal", async () => {
    let resolveRenewal: ((tokens: { accessToken: string; expiresInSeconds: number; refreshToken: string }) => void) | undefined;
    const renewal = new Promise<{ accessToken: string; expiresInSeconds: number; refreshToken: string }>((resolve) => {
      resolveRenewal = resolve;
    });
    const session = new MemoryAuthSession();
    session.setRenewer(() => renewal);
    session.set("expired", 1, "memory-refresh");
    const pending = session.renewAccessToken();
    session.clear();
    resolveRenewal?.({ accessToken: "late-access", expiresInSeconds: 3600, refreshToken: "late-refresh" });
    await expect(pending).resolves.toBeNull();
    expect(session.getAccessToken()).toBeNull();
    expect(session.getStatus()).toBe("anonymous");
  });

  it.each([
    ["missing code", (state: string) => `?state=${state}`],
    ["duplicate code", (state: string) => `?code=first&code=second&state=${state}`],
    ["missing state", () => "?code=one-use"],
  ])("consumes the PKCE transaction when callback data has %s", async (_label, callback) => {
    const coordinator = new OAuthCoordinator(config, new MemoryAuthSession(), window.sessionStorage, vi.fn(), vi.fn());
    await coordinator.startSignIn("/jobs/job_123");
    const stored = JSON.parse(window.sessionStorage.getItem("mr-lister.oauth-transaction.v1") ?? "null") as { state: string };
    window.history.replaceState(null, "", `/auth/callback${callback(stored.state)}`);
    await expect(coordinator.completeSignIn(window.location.search)).rejects.toThrow();
    expect(window.location.search).toBe("");
    expect(window.sessionStorage.length).toBe(0);
  });
});
