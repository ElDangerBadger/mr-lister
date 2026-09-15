import { afterEach, describe, expect, it, vi } from "vitest";
import { runtimeConfigSchemaForOrigin, type RuntimeConfig } from "../src/contracts";
import { MemoryAuthSession, OAuthCoordinator } from "../src/auth/session";
import { judgeSignOutTarget, loadRuntimeConfig, workspaceBasePath } from "../src/runtime";

const origin = window.location.origin;
const sellerConfig: RuntimeConfig = {
  cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
  cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
  cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
  client_id: "primaryclient",
  redirect_uri: `${origin}/auth/callback`,
  scopes: ["openid", "mr-lister-api/seller"],
};
const judgeConfig: RuntimeConfig = {
  ...sellerConfig,
  redirect_uri: `${origin}/judge/auth/callback`,
  judge_access: {
    identity_provider: "MrListerJudge",
    upstream_logout_url: "https://judges.auth.us-west-2.amazoncognito.com/logout",
    upstream_client_id: "judgeclient",
    prepared_job_id: "job_example",
  },
};

afterEach(() => { vi.restoreAllMocks(); });

describe("judge authentication boundary", () => {
  it("selects the judge config only from its exact path prefix", async () => {
    expect(workspaceBasePath("/judge")).toBe("/judge");
    expect(workspaceBasePath("/judge/jobs/job_1")).toBe("/judge");
    expect(workspaceBasePath("/judges")).toBe("");
    expect(workspaceBasePath("/jobs/judge")).toBe("");
    window.history.replaceState(null, "", "/judge/jobs/job_example");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(judgeConfig)));
    await expect(loadRuntimeConfig(fetcher)).resolves.toEqual(judgeConfig);
    expect(fetcher.mock.calls[0]?.[0]).toBe("/judge/runtime-config.json");
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({ credentials: "omit", redirect: "error", cache: "no-store" });
  });

  it("rejects mixed seller/judge configurations and untrusted sign-out targets", () => {
    const judgeSchema = runtimeConfigSchemaForOrigin(origin, "/judge");
    expect(judgeSchema.safeParse(judgeConfig).success).toBe(true);
    expect(judgeSchema.safeParse(sellerConfig).success).toBe(false);
    expect(runtimeConfigSchemaForOrigin(origin).safeParse(judgeConfig).success).toBe(false);
    for (const invalid of [
      { ...judgeConfig, redirect_uri: `${origin}/auth/callback` },
      { ...judgeConfig, redirect_uri: `${origin}/judge/auth/callback?redirect=evil` },
      { ...judgeConfig, judge_access: { ...judgeConfig.judge_access, identity_provider: "OtherProvider" } },
      { ...judgeConfig, judge_access: { ...judgeConfig.judge_access, upstream_logout_url: "https://evil.example/logout" } },
      { ...judgeConfig, judge_access: { ...judgeConfig.judge_access, upstream_logout_url: sellerConfig.cognito_logout_url } },
      { ...judgeConfig, judge_access: { ...judgeConfig.judge_access, client_secret: "must-never-be-public" } },
    ]) expect(judgeSchema.safeParse(invalid).success).toBe(false);
  });

  it("uses the broker provider with existing client, scopes and PKCE for full-page sign-in", async () => {
    const targets: URL[] = [];
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      access_token: "judge-token", expires_in: 3600, token_type: "Bearer",
    })));
    const auth = new OAuthCoordinator(judgeConfig, new MemoryAuthSession(), window.sessionStorage, fetcher, (url) => targets.push(url));
    await auth.startSignIn("/jobs/job_example");
    const target = targets[0]!;
    expect(target.searchParams.get("identity_provider")).toBe("MrListerJudge");
    expect(target.searchParams.get("client_id")).toBe(sellerConfig.client_id);
    expect(target.searchParams.get("code_challenge_method")).toBe("S256");
    expect(target.searchParams.get("scope")).toBe(sellerConfig.scopes.join(" "));
    expect(target.searchParams.get("redirect_uri")).toBe(judgeConfig.redirect_uri);
    const search = `?code=judge-code&state=${target.searchParams.get("state")}`;
    window.history.replaceState(null, "", `/judge/auth/callback${search}`);
    await expect(auth.completeSignIn(search)).resolves.toBe("/jobs/job_example");
    expect(window.location.pathname).toBe("/judge/auth/callback");
    expect(window.location.search).toBe("");
    expect(sessionStorage.length).toBe(0);
    expect((fetcher.mock.calls[0]?.[1]?.body as URLSearchParams).get("redirect_uri")).toBe(judgeConfig.redirect_uri);
  });

  it("keeps ordinary seller sign-in routed to the local MFA-protected pool", async () => {
    const targets: URL[] = [];
    const auth = new OAuthCoordinator(sellerConfig, new MemoryAuthSession(), window.sessionStorage, vi.fn(), (url) => targets.push(url));
    await auth.startSignIn("/");
    expect(targets[0]?.searchParams.get("identity_provider")).toBe("COGNITO");
    expect(targets[0]?.searchParams.get("redirect_uri")).toBe(sellerConfig.redirect_uri);
  });

  it("clears application tokens and chains sign-out through both fixed identity services", () => {
    const targets: URL[] = [];
    const session = new MemoryAuthSession();
    session.set("access", 3600, "refresh");
    const auth = new OAuthCoordinator(judgeConfig, session, window.sessionStorage, vi.fn(), (url) => targets.push(url));
    auth.signOut();
    expect(session.getStatus()).toBe("anonymous");
    expect(targets[0]?.origin).toBe(new URL(sellerConfig.cognito_logout_url).origin);
    expect(targets[0]?.searchParams.get("logout_uri")).toBe(`${origin}/judge/signout`);
    const upstream = judgeSignOutTarget(judgeConfig, "/judge/signout")!;
    expect(upstream.origin).toBe("https://judges.auth.us-west-2.amazoncognito.com");
    expect(upstream.searchParams.get("client_id")).toBe("judgeclient");
    expect(upstream.searchParams.get("logout_uri")).toBe(`${origin}/judge/`);
    expect(judgeSignOutTarget(sellerConfig, "/judge/signout")).toBeNull();
    expect(judgeSignOutTarget(judgeConfig, "/judge/other")).toBeNull();
  });
});
