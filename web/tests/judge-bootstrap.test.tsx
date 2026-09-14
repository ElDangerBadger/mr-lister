import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountApplication } from "../src/bootstrap";
import type { RuntimeConfig } from "../src/contracts";

const sellerConfig: RuntimeConfig = {
  cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
  cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
  cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
  client_id: "primaryclient",
  redirect_uri: `${window.location.origin}/auth/callback`,
  scopes: ["openid", "mr-lister-api/seller"],
};
const judgeConfig: RuntimeConfig = {
  ...sellerConfig,
  redirect_uri: `${window.location.origin}/judge/auth/callback`,
  judge_access: {
    identity_provider: "MrListerJudge",
    upstream_logout_url: "https://judges.auth.us-west-2.amazoncognito.com/logout",
    upstream_client_id: "judgeclient",
    prepared_job_id: "job_example",
  },
};
const claims = {
  token_use: "access",
  client_id: sellerConfig.client_id,
  iss: "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_ExamplePool",
  "cognito:groups": ["seller", "us-west-2_ExamplePool_MrListerJudge"],
};
const accessToken = `header.${btoa(JSON.stringify(claims)).replace(/=/gu, "").replace(/\+/gu, "-").replace(/\//gu, "_")}.signature`;
const refreshToken = "bootstrap-refresh-secret";
const transactionKey = "mr-lister.oauth-transaction.v1";
let root: Root | undefined;
let container: HTMLDivElement | undefined;

afterEach(() => {
  act(() => { root?.unmount(); });
  container?.remove();
  root = undefined;
  container = undefined;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.name = "";
});

function mockEndpoints() {
  let resolveConfig!: (response: Response) => void;
  const pendingConfig = new Promise<Response>((resolve) => { resolveConfig = resolve; });
  const reads: { pathname: string; judgeBanner: boolean; authorization: string | null }[] = [];
  const fetcher = vi.fn<typeof fetch>((input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.origin);
    if (url.href === sellerConfig.cognito_token_url) {
      return Promise.resolve(new Response(JSON.stringify({
        access_token: accessToken, refresh_token: refreshToken,
        expires_in: 3600, token_type: "Bearer",
      })));
    }
    if (url.pathname === "/judge/runtime-config.json") return pendingConfig;
    if (url.pathname === "/v1/jobs" && url.search === "?limit=25") {
      reads.push({
        pathname: window.location.pathname,
        judgeBanner: screen.queryByRole("complementary", { name: "Judge walkthrough" }) !== null,
        authorization: new Headers(init?.headers).get("Authorization"),
      });
      return Promise.resolve(new Response(JSON.stringify({ jobs: [], next_cursor: null }), {
        headers: { "Content-Type": "application/json", "X-Request-Id": "bootstrap-jobs" },
      }));
    }
    return Promise.reject(new Error(`Unexpected test request: ${url.pathname}`));
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    fetcher,
    reads,
    resolveConfig: () => { resolveConfig(new Response(JSON.stringify(judgeConfig))); },
  };
}

async function mountSeller() {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    mountApplication(root!, sellerConfig);
    await Promise.resolve();
  });
}

function storageValues(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, index) => storage.getItem(storage.key(index)!)).join(" ");
}

async function expectJudgeWorkspace(endpoints: ReturnType<typeof mockEndpoints>) {
  await screen.findByRole("navigation", { name: "Judge resources" });
  await waitFor(() => { expect(endpoints.reads.length).toBeGreaterThan(0); });
  expect(window.location.pathname).toBe("/judge");
  expect(screen.getByText("Signed in")).toBeVisible();
  expect(screen.getByRole("complementary", { name: "Judge walkthrough" })).toBeVisible();
  expect(screen.getByRole("link", { name: "Mr. Lister seller review home" })).toHaveAttribute("href", "/judge");
  expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", "/judge");
  expect(screen.getByRole("link", { name: "Review prepared example" })).toHaveAttribute("href", "/judge/jobs/job_example");
  expect(screen.queryByRole("heading", { name: "Sign-in needs another try" })).not.toBeInTheDocument();
  expect(endpoints.reads.every((read) => read.pathname === "/judge" && read.judgeBanner && read.authorization === `Bearer ${accessToken}`)).toBe(true);
  expect(endpoints.fetcher.mock.calls.filter(([input]) => input === sellerConfig.cognito_token_url)).toHaveLength(1);
  const exchange = endpoints.fetcher.mock.calls.find(([input]) => input === sellerConfig.cognito_token_url)!;
  // The code belongs to the original root callback, even though the resulting workspace is judge mode.
  expect((exchange[1]?.body as URLSearchParams).get("redirect_uri")).toBe(sellerConfig.redirect_uri);
  const configReads = endpoints.fetcher.mock.calls.filter(([input]) => input === "/judge/runtime-config.json");
  expect(configReads).toHaveLength(1);
  expect(configReads[0]?.[1]).toMatchObject({ credentials: "omit", cache: "no-store", redirect: "error" });
  expect(new Headers(configReads[0]?.[1]?.headers).has("Authorization")).toBe(false);
  expect(window.sessionStorage.getItem(transactionKey)).toBeNull();
  for (const value of [window.location.href, storageValues(window.sessionStorage), storageValues(window.localStorage)]) {
    expect(value).not.toContain(accessToken);
    expect(value).not.toContain(refreshToken);
  }
  expect(window.location.search).toBe("");
}

describe("real application bootstrap after judge federation from seller login", () => {
  it("remounts the root popup flow into judge mode before exposing the session or reading jobs", async () => {
    const endpoints = mockEndpoints();
    const popupValues = new Map<string, string>();
    const popup = {
      closed: false,
      focus: vi.fn(),
      close: vi.fn(() => { popup.closed = true; }),
      postMessage: vi.fn(),
      location: { replace: vi.fn() },
      sessionStorage: {
        removeItem: (key: string) => { popupValues.delete(key); },
        setItem: (key: string, value: string) => { popupValues.set(key, value); },
      },
    };
    const handle = popup as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(handle);
    const replace = vi.spyOn(window.history, "replaceState");
    const push = vi.spyOn(window.history, "pushState");
    await mountSeller();
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => { expect(popup.location.replace).toHaveBeenCalledOnce(); });
    const authorization = new URL(popup.location.replace.mock.calls[0]![0] as string);
    expect(authorization.searchParams.get("redirect_uri")).toBe(sellerConfig.redirect_uri);
    expect(authorization.searchParams.get("identity_provider")).toBe("COGNITO");
    await act(async () => {
      window.dispatchEvent(new MessageEvent("message", {
        origin: window.location.origin,
        source: handle,
        data: { type: "mr-lister.oauth-popup-callback.v1", search: `?code=popup-one-use&state=${authorization.searchParams.get("state")}` },
      }));
      await Promise.resolve();
    });
    await waitFor(() => { expect(endpoints.fetcher).toHaveBeenCalledWith("/judge/runtime-config.json", expect.anything()); });
    expect(endpoints.reads).toEqual([]);
    expect(window.location.pathname).toBe("/");
    expect(screen.queryByText("Signed in")).not.toBeInTheDocument();
    await act(async () => {
      endpoints.resolveConfig();
      await Promise.resolve();
    });
    await expectJudgeWorkspace(endpoints);
    // The retired SignInProvider must neither cancel the accepted token nor navigate using its old root router.
    expect(popup.close).toHaveBeenCalledOnce();
    expect(replace.mock.calls.flatMap((call) => call[2] === undefined ? [] : [call[2]])).toEqual(["/judge"]);
    expect(push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(JSON.stringify([...popupValues.values()])).not.toContain(accessToken);
    expect(JSON.stringify([...popupValues.values()])).not.toContain(refreshToken);
  });

  it("consumes a root full-page callback once and keeps the remounted judge router after its promise settles", async () => {
    const endpoints = mockEndpoints();
    const state = "s".repeat(43);
    sessionStorage.setItem(transactionKey, JSON.stringify({ state, verifier: "v".repeat(64), returnPath: "/" }));
    window.history.replaceState(null, "", `/auth/callback?code=redirect-one-use&state=${state}`);
    const replace = vi.spyOn(window.history, "replaceState");
    const push = vi.spyOn(window.history, "pushState");
    await mountSeller();
    await waitFor(() => { expect(endpoints.fetcher).toHaveBeenCalledWith("/judge/runtime-config.json", expect.anything()); });
    expect(endpoints.reads).toEqual([]);
    expect(window.location.pathname).toBe("/auth/callback");
    expect(window.location.search).toBe("");
    expect(screen.queryByText("Signed in")).not.toBeInTheDocument();
    await act(async () => {
      endpoints.resolveConfig();
      await Promise.resolve();
    });
    await expectJudgeWorkspace(endpoints);
    // StrictMode and the old callback page must not repeat the exchange or navigate back to seller mode.
    expect(replace.mock.calls.flatMap((call) => call[2] === undefined ? [] : [call[2]])).toEqual(["/auth/callback", "/judge"]);
    expect(push).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Verifying your session…" })).not.toBeInTheDocument();
  });
});
