import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import axe from "axe-core";
import { AppRoutes } from "../src/App";
import type { ApiPort } from "../src/api/client";
import { AuthError, MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { AccountSetupError, BrowserAccountSetupClient, accountIdentity, accountSetupSchema, type AccountSetup, type AccountWorkspaceConfiguration } from "../src/auth/account-workspace";

const config: AccountWorkspaceConfiguration = {
  clientId: "publicclient", cognitoTokenUrl: "https://sellers.auth.us-west-2.amazoncognito.com/oauth2/token",
  issuer: "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Sellers", selfServiceSignup: true, supportEmail: "support@example.com", noticeVersion: "2026-10-05",
};
const setup: AccountSetup = { contract_version: "account-setup-v1", record_version: 1, state: "connection_unavailable", connection_method: "unavailable", store: null };
const ready: AccountSetup = { ...setup, state: "ready", connection_method: "personal_token", store: { connection_id: `conn_${"a".repeat(32)}`, shop_binding_id: `binding_${"b".repeat(32)}`, shop_id: 123, name: "My Etsy store", sales_channel: "etsy" } };
function token(subject = "seller-one", groups = ["account"], extra: Record<string, unknown> = {}) {
  return `header.${btoa(JSON.stringify({ sub: subject, iss: config.issuer, client_id: config.clientId, token_use: "access", "cognito:groups": groups, ...extra })).replace(/=/gu, "").replace(/\+/gu, "-").replace(/\//gu, "_")}.signature`;
}
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }

describe("account setup boundary", () => {
  it("treats JWT account hints as presentation only and requires the expected authority", () => {
    expect(accountIdentity(token(), config)).toBe(`${config.issuer}\0seller-one`);
    for (const candidate of [token("owner", ["seller"]), token("judge", ["seller", "us-west-2_Sellers_MrListerJudge"]), token("wrong", ["account"], { client_id: "other" }), token("wrong", ["account"], { iss: "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Other" }), token("wrong", ["account"], { token_use: "id" }), "bad-token"]) expect(accountIdentity(candidate, config)).toBeNull();
    expect(accountIdentity(token(), { ...config, selfServiceSignup: false })).not.toBeNull();
  });

  it.each(["/", "/jobs/job_private", "/uploads/upload_private", "/store-setup"])("keeps new accounts at setup on %s without any seller API calls", async (route) => {
    const fixture = renderAccount({ route });
    await screen.findByRole("heading", { name: "Your account is ready." });
    expect(screen.getByText("Store connections coming soon")).toBeVisible();
    expect(screen.queryByLabelText(/Drag and drop/u)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("route")).toHaveTextContent(/^\/store-setup$/u));
    for (const method of Object.values(fixture.api)) expect(method).not.toHaveBeenCalled();
  });

  it("does not unlock upload or job routes from a ready response before workflow wiring exists", async () => {
    const fixture = renderAccount({ value: ready, route: "/jobs/job_private" });
    await screen.findByText(/My Etsy store is connected/u);
    expect(screen.queryByRole("button", { name: /upload|publish/iu })).not.toBeInTheDocument();
    for (const method of Object.values(fixture.api)) expect(method).not.toHaveBeenCalled();
  });

  it("preserves account setup through a same-identity token renewal", async () => {
    const fixture = renderAccount();
    await screen.findByRole("heading", { name: "Your account is ready." });
    act(() => { fixture.session.set(token("seller-one", ["account"], { jti: "renewed" }), 3600); });
    expect(screen.getByRole("heading", { name: "Your account is ready." })).toBeVisible();
    expect(fixture.getSetup).toHaveBeenCalledOnce();
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it("keeps the gate closed while an expired token renews and passes accessibility checks", async () => {
    const fixture = renderAccount();
    await screen.findByRole("heading", { name: "Your account is ready." });
    const renewal = deferred<{ accessToken: string; expiresInSeconds: number }>();
    fixture.session.setRenewer(() => renewal.promise);
    act(() => { fixture.session.set(token(), 1, "memory-refresh"); });
    expect(screen.getByRole("heading", { name: "Your account is ready." })).toBeVisible();
    const pending = fixture.session.renewAccessToken();
    await act(async () => { renewal.resolve({ accessToken: token("seller-one", ["account"], { jti: "next" }), expiresInSeconds: 3600 }); await pending; });
    expect(fixture.getSetup).toHaveBeenCalledOnce();
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
    expect((await axe.run(document.querySelector(".app-shell")!, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("ignores a late account response after logout", async () => {
    const pending = deferred<AccountSetup>();
    const getSetup = vi.fn().mockReturnValue(pending.promise);
    const fixture = renderAccount({ getSetup });
    const signal = getSetup.mock.calls[0]![1] as AbortSignal;
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(signal.aborted).toBe(true);
    await act(async () => { pending.resolve(ready); await pending.promise; });
    expect(screen.queryByText(/My Etsy store/u)).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your account is ready." })).not.toBeInTheDocument();
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it("cancels stale setup results on identity changes and logout", async () => {
    const old = deferred<AccountSetup>();
    const getSetup = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue(setup);
    const fixture = renderAccount({ getSetup });
    const oldSignal = getSetup.mock.calls[0]![1] as AbortSignal;
    act(() => { fixture.session.set(token("seller-two"), 3600); });
    await screen.findByRole("heading", { name: "Your account is ready." });
    expect(oldSignal.aborted).toBe(true);
    await act(async () => { old.resolve(ready); await old.promise; });
    expect(screen.queryByText(/My Etsy store/u)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("heading", { name: /Your artwork\.\s*Your next listing/u });
    expect(fixture.auth.signOut).toHaveBeenCalledOnce();
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it("shows a safe retry without reflecting API details", async () => {
    const getSetup = vi.fn().mockRejectedValueOnce(new Error("private-provider-response")).mockResolvedValue(setup);
    renderAccount({ getSetup });
    expect(await screen.findByRole("alert")).not.toHaveTextContent("private-provider-response");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("heading", { name: "Your account is ready." });
  });

  it("keeps the existing owner workspace without querying account setup", async () => {
    const fixture = renderAccount({ groups: ["seller"] });
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    await waitFor(() => expect(fixture.api.listJobs).toHaveBeenCalledOnce());
    expect(fixture.getSetup).not.toHaveBeenCalled();
  });

  it("offers signup only when enabled and retains signup when falling back from a blocked popup", async () => {
    const fixture = renderAccount({ anonymous: true });
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("blocked");
    expect(fixture.auth.startPopupSignUp).toHaveBeenCalledWith("/store-setup");
    await userEvent.click(screen.getByRole("button", { name: "Continue in this tab" }));
    expect(fixture.auth.startSignUp).toHaveBeenCalledWith("/store-setup");
    expect(fixture.auth.startSignIn).not.toHaveBeenCalled();
  });

  it("does not offer signup when disabled but still gates an account-group sign-in", async () => {
    const fixture = renderAccount({ anonymous: true, enabled: false });
    expect(screen.queryByRole("button", { name: "Create account" })).not.toBeInTheDocument();
    act(() => { fixture.session.set(token(), 3600); });
    await screen.findByRole("heading", { name: "Your account is ready." });
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it.each(["supportEmail", "noticeVersion"] as const)("does not offer public signup without %s", (missing) => {
    const accountConfig = { ...config }; delete accountConfig[missing];
    const fixture = renderAccount({ anonymous: true, accountConfig });
    expect(screen.queryByRole("button", { name: "Create account" })).not.toBeInTheDocument();
    expect(fixture.auth.startSignUp).not.toHaveBeenCalled();
  });
});

describe("account API client", () => {
  it("uses a fixed authenticated GET with strict bounded response validation", async () => {
    const session = new MemoryAuthSession(); session.set(token(), 3600);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(setup));
    const client = new BrowserAccountSetupClient(session, config, fetcher);
    const signal = new AbortController().signal;
    await expect(client.getSetup(accountIdentity(token(), config)!, signal)).resolves.toEqual(setup);
    expect(fetcher).toHaveBeenCalledWith("/v1/account", expect.objectContaining({ method: "GET", signal, cache: "no-store", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer", headers: { Accept: "application/json", Authorization: `Bearer ${token()}` } }));
    expect(window.sessionStorage.length).toBe(0);
  });

  it.each([response({ ...setup, secret: "private-token" }), response({ ...setup, state: "ready" }), response({ private: "private-token" }, 500), new Response("x".repeat(16_385), { headers: { "Content-Type": "application/json" } })])("rejects invalid, oversized or failed responses with a fixed safe error", async (value) => {
    const session = new MemoryAuthSession(); session.set(token(), 3600);
    const client = new BrowserAccountSetupClient(session, config, vi.fn<typeof fetch>().mockResolvedValue(value));
    await expect(client.getSetup(accountIdentity(token(), config)!, new AbortController().signal)).rejects.toEqual(new AccountSetupError("unavailable"));
  });

  it("renews once on 401 and refuses a response for an identity that changed while loading", async () => {
    const session = new MemoryAuthSession(); session.set(token(), 3600, "memory-refresh");
    const renewed = token("seller-one", ["account"], { jti: "next" });
    session.setRenewer(vi.fn().mockResolvedValue({ accessToken: renewed, expiresInSeconds: 3600 }));
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({}, 401)).mockResolvedValueOnce(response(setup));
    const client = new BrowserAccountSetupClient(session, config, fetcher);
    await expect(client.getSetup(accountIdentity(token(), config)!, new AbortController().signal)).resolves.toEqual(setup);
    expect(fetcher).toHaveBeenCalledTimes(2);
    const pending = deferred<Response>(); fetcher.mockReturnValueOnce(pending.promise);
    const result = client.getSetup(accountIdentity(renewed, config)!, new AbortController().signal);
    session.set(token("other"), 3600);
    pending.resolve(response(setup));
    await expect(result).rejects.toEqual(new AccountSetupError("session_expired"));
  });

  it("rejects invalid store summaries and incompatible state combinations", () => {
    expect(accountSetupSchema.safeParse(ready).success).toBe(true);
    expect(accountSetupSchema.safeParse({ ...ready, connection_method: "unavailable" }).success).toBe(false);
    expect(accountSetupSchema.safeParse({ ...ready, store: { ...ready.store, sales_channel: "shopify" } }).success).toBe(false);
    expect(accountSetupSchema.safeParse({ ...setup, record_version: 0 }).success).toBe(false);
  });
});

function CurrentRoute() { return <p data-testid="route">{useLocation().pathname}</p>; }
function renderAccount(options: { route?: string; groups?: string[]; anonymous?: boolean; enabled?: boolean; value?: AccountSetup; getSetup?: ReturnType<typeof vi.fn>; accountConfig?: AccountWorkspaceConfiguration } = {}) {
  const session = new MemoryAuthSession();
  if (!options.anonymous) session.set(token("seller-one", options.groups ?? ["account"]), 3600);
  const unused = () => vi.fn().mockRejectedValue(new Error("Unexpected seller API call"));
  const api = { listJobs: vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "jobs", etag: null }), clearRecentJobs: unused(), getJob: unused(), getUpload: unused(), getReview: unused(), createUpload: unused(), authorizeUpload: unused(), completeUpload: unused(), cancelUpload: unused(), reviseListing: unused(), runAction: unused(), fetchArtwork: unused() } satisfies ApiPort;
  const getSetup = options.getSetup ?? vi.fn().mockResolvedValue(options.value ?? setup);
  const auth = { session, startSignIn: vi.fn().mockResolvedValue(undefined), startSignUp: vi.fn().mockResolvedValue(undefined), startPopupSignUp: vi.fn().mockRejectedValue(new AuthError("Your browser blocked the secure window.")), cancelPopupSignIn: vi.fn(), completeSignIn: vi.fn().mockResolvedValue("/"), signOut: vi.fn(() => { session.clear(); }) } satisfies AuthCoordinator;
  render(<MemoryRouter initialEntries={[options.route ?? "/"]}><AppRoutes dependencies={{ auth, api, accountApi: { getSetup }, accountConfig: options.accountConfig ?? { ...config, selfServiceSignup: options.enabled !== false } }} /><CurrentRoute /></MemoryRouter>);
  return { session, api, auth, getSetup };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}
