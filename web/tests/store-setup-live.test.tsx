import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import axe from "axe-core";
import { AppRoutes } from "../src/App";
import type { ApiPort } from "../src/api/client";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { BrowserAccountSetupClient, type AccountSetup, type AccountWorkspaceConfiguration } from "../src/auth/account-workspace";
import { LiveStoreConnectionAdapter } from "../src/store-setup/live-adapter";
import { StoreConnectionError } from "../src/store-setup/connection-adapter";
import { SERVICE_NOTICE_VERSION, type MerchantAuthorization } from "../src/service-notices";

const config: AccountWorkspaceConfiguration = { clientId: "publicclient", cognitoTokenUrl: "https://sellers.auth.us-west-2.amazoncognito.com/oauth2/token", issuer: "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Sellers", selfServiceSignup: true, supportEmail: "support@example.com", noticeVersion: "2026-10-05", connectionMethod: "personal_token", connectedWorkflow: true };
const initial: AccountSetup = { contract_version: "account-setup-v1", record_version: 1, state: "connection_required", connection_method: "personal_token", store: null };
const ready: AccountSetup = { ...initial, record_version: 2, state: "ready", store: { connection_id: `conn_${"a".repeat(32)}`, shop_binding_id: `binding_${"b".repeat(32)}`, shop_id: 123, name: "My Etsy store", sales_channel: "etsy" } };
const authorization: MerchantAuthorization = { accepted: true, terms_version: SERVICE_NOTICE_VERSION, privacy_version: SERVICE_NOTICE_VERSION };
const candidateId = `candidate_${"c".repeat(32)}`;
const candidate = () => ({ candidate_id: candidateId, record_version: 1, expires_at: new Date(Date.now() + 900_000).toISOString(), stores: [{ id: "123", name: "My Etsy store", sales_channel: "etsy", eligible: true, disabled_reason: null }, { id: "456", name: "Other channel", sales_channel: "shopify", eligible: false, disabled_reason: "unsupported_channel" }] });
function token(subject = "account-one", groups = ["account"], extra: Record<string, unknown> = {}) { return `header.${btoa(JSON.stringify({ sub: subject, iss: config.issuer, client_id: config.clientId, token_use: "access", "cognito:groups": groups, ...extra })).replace(/=/gu, "").replace(/\+/gu, "-").replace(/\//gu, "_")}.signature`; }
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
const adapters: LiveStoreConnectionAdapter[] = [];
afterEach(() => { for (const adapter of adapters.splice(0)) adapter.reset(); });
function client(fetcher: typeof fetch, options: { configuration?: AccountWorkspaceConfiguration; groups?: string[]; now?: () => number } = {}) {
  const session = new MemoryAuthSession(); session.set(token("account-one", options.groups ?? ["account"]), 3600, "memory-refresh");
  const renew = vi.fn().mockResolvedValue({ accessToken: token("account-one", ["account", "seller"]), expiresInSeconds: 3600 }); session.setRenewer(renew);
  const adapter = new LiveStoreConnectionAdapter(session, options.configuration ?? config, fetcher, options.now);
  adapters.push(adapter); return { session, renew, adapter };
}

describe("live store connection adapter", () => {
  it.each([undefined, { ...authorization, accepted: false }, { ...authorization, terms_version: "old-version" }, { ...authorization, extra: true }])("requires exact current merchant authorization before any request", async (consent) => {
    const fetcher = vi.fn<typeof fetch>(); const { adapter } = client(fetcher);
    await expect(adapter.validate("key", consent as MerchantAuthorization, new AbortController().signal)).rejects.toMatchObject({ code: "authorization_required" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps candidates in memory and sends only fixed authenticated connection requests", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(candidate())).mockResolvedValueOnce(response(ready));
    const { adapter, renew } = client(fetcher); const signal = new AbortController().signal;
    const validated = await adapter.validate("transient-printify-key", authorization, signal);
    const stores = await adapter.listStores(validated.validationId, signal);
    expect(stores[1]).toMatchObject({ eligible: false, disabledReason: "unsupported_channel" });
    const result = await adapter.connect(validated.validationId, "123", signal);
    expect(result.setup).toEqual(ready); expect(renew).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(["/v1/connections/printify/validate", "/v1/connections/printify/select-shop"]);
    expect(JSON.parse(fetcher.mock.calls[0]![1]!.body as string)).toEqual({ token: "transient-printify-key", authorization });
    expect(JSON.parse(fetcher.mock.calls[1]![1]!.body as string)).toEqual({ candidate_id: candidateId, shop_id: 123, expected_setup_version: 1 });
    for (const [, options] of fetcher.mock.calls) {
      expect(options).toMatchObject({ signal, credentials: "omit", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer" });
      expect(new Headers(options?.headers).get("Authorization")).toBe(`Bearer ${token()}`);
      expect(new Headers(options?.headers).get("Idempotency-Key")).toBeTruthy();
    }
    expect(window.localStorage.length).toBe(0); expect(window.sessionStorage.length).toBe(0);
    await expect(adapter.listStores(candidateId, signal)).rejects.toMatchObject({ code: "validation_expired" });
  });

  it("retains an uncertain save key for the same selection and rejects unsupported stores", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(candidate())).mockRejectedValueOnce(new Error("private-provider-details")).mockResolvedValueOnce(response(ready));
    const { adapter } = client(fetcher); const signal = new AbortController().signal;
    await adapter.validate("key", authorization, signal);
    await expect(adapter.connect(candidateId, "456", signal)).rejects.toBeInstanceOf(StoreConnectionError);
    expect(fetcher).toHaveBeenCalledOnce();
    await expect(adapter.connect(candidateId, "123", signal)).rejects.toMatchObject({ code: "connection_failed" });
    await adapter.connect(candidateId, "123", signal);
    expect(fetcher.mock.calls[1]?.[1]?.body).toEqual(fetcher.mock.calls[2]?.[1]?.body);
    expect(new Headers(fetcher.mock.calls[1]?.[1]?.headers).get("Idempotency-Key")).toBe(new Headers(fetcher.mock.calls[2]?.[1]?.headers).get("Idempotency-Key"));
  });

  it("rejects candidates after account changes, cancellation or expiry", async () => {
    let now = Date.now();
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(response(candidate())));
    const { adapter, session } = client(fetcher, { now: () => now }); const controller = new AbortController();
    await adapter.validate("key", authorization, controller.signal);
    session.set(token("other-account"), 3600);
    await expect(adapter.listStores(candidateId, controller.signal)).rejects.toMatchObject({ code: "validation_expired" });
    session.set(token(), 3600); await adapter.validate("key", authorization, controller.signal);
    now += 901_000;
    await expect(adapter.listStores(candidateId, controller.signal)).rejects.toMatchObject({ code: "validation_expired" });
    now = Date.now(); await adapter.validate("key", authorization, controller.signal); controller.abort();
    await expect(adapter.listStores(candidateId, controller.signal)).rejects.toMatchObject({ code: "session_expired" });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("ignores a validation response delivered after reset", async () => {
    const pending = deferred<Response>(); const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    const { adapter } = client(fetcher); const result = adapter.validate("key", authorization, new AbortController().signal);
    await waitFor(() => expect(fetcher).toHaveBeenCalledOnce()); adapter.reset(); pending.resolve(response(candidate()));
    await expect(result).rejects.toMatchObject({ code: "connection_failed" });
    await expect(adapter.listStores(candidateId, new AbortController().signal)).rejects.toMatchObject({ code: "validation_expired" });
  });

  it("discards an old account’s response after identity changes during a request", async () => {
    const pending = deferred<Response>(); const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    const { adapter, session } = client(fetcher); const signal = new AbortController().signal;
    const result = adapter.validate("key", authorization, signal);
    await waitFor(() => expect(fetcher).toHaveBeenCalledOnce()); session.set(token("other-account"), 3600); pending.resolve(response(candidate()));
    await expect(result).rejects.toMatchObject({ code: "session_expired" });
    session.set(token(), 3600);
    await expect(adapter.listStores(candidateId, signal)).rejects.toMatchObject({ code: "validation_expired" });
  });

  it.each(["unknown_field", "ineligible_etsy", "duplicate_shop", "expired", "oversized"])("rejects a malformed candidate: %s", async (variation) => {
    const value = candidate();
    const body: unknown = variation === "unknown_field" ? { ...value, token: "must-never-return" }
      : variation === "ineligible_etsy" ? { ...value, stores: [{ ...value.stores[1], eligible: true, disabled_reason: null }] }
      : variation === "duplicate_shop" ? { ...value, stores: [value.stores[0], value.stores[0]] }
      : variation === "expired" ? { ...value, expires_at: new Date(Date.now() - 1).toISOString() } : { padding: "x".repeat(65_537) };
    const { adapter } = client(vi.fn<typeof fetch>().mockResolvedValue(response(body)));
    await expect(adapter.validate("key", authorization, new AbortController().signal)).rejects.toBeInstanceOf(StoreConnectionError);
  });

  it.each([["seller"], ["seller", "judge"]])("never calls connection APIs for an existing owner or judge: %j", async (...groups) => {
    const fetcher = vi.fn<typeof fetch>(); const { adapter } = client(fetcher, { groups });
    await expect(adapter.validate("key", authorization, new AbortController().signal)).rejects.toMatchObject({ code: "session_expired" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("requires both ready server state and refreshed membership before reporting success", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response(candidate())).mockResolvedValueOnce(response(ready));
    const { adapter, renew } = client(fetcher); renew.mockResolvedValue({ accessToken: token(), expiresInSeconds: 3600 });
    const signal = new AbortController().signal; await adapter.validate("key", authorization, signal);
    await expect(adapter.connect(candidateId, "123", signal)).rejects.toMatchObject({ code: "session_expired" });
    expect(renew).toHaveBeenCalledOnce();
  });

  it("refuses activation if forced renewal returns another account’s seller token", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(ready)); const { adapter, renew } = client(fetcher);
    renew.mockResolvedValue({ accessToken: token("different-account", ["account", "seller"]), expiresInSeconds: 3600 });
    await expect(adapter.activate(new AbortController().signal)).rejects.toMatchObject({ code: "session_expired" });
  });

  it("finishes a saved binding without a token or candidate and refuses disabled workflow", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(response(ready)));
    const first = client(fetcher); const signal = new AbortController().signal;
    expect((await first.adapter.activate(signal)).setup).toEqual(ready);
    expect(fetcher.mock.calls[0]?.[0]).toBe("/v1/connections/printify/activate");
    expect(fetcher.mock.calls[0]?.[1]?.body).toBe("{}");
    expect(first.renew).toHaveBeenCalledOnce();
    const disabled = client(fetcher, { configuration: { ...config, connectedWorkflow: false } });
    await expect(disabled.adapter.activate(signal)).rejects.toMatchObject({ code: "setup_unavailable" });
    expect(disabled.renew).not.toHaveBeenCalled();
  });
});

describe("account to store setup", () => {
  it("restores account status first, clears the key, keeps selection through renewal and opens the bound workflow only on Enter", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation((path) => Promise.resolve(response(path === "/v1/store-setup" ? initial : path === "/v1/connections/printify/validate" ? candidate() : ready)));
    const fixture = renderSetup(fetcher);
    const input = await screen.findByLabelText("Connection token");
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(["/v1/store-setup"]);
    await userEvent.type(input, "private-transient-key");
    expect(screen.getByRole("button", { name: "Find my stores" })).toBeDisabled();
    const consent = screen.getByRole("checkbox", { name: /I’m authorized to connect this Printify account/u });
    expect(consent).not.toBeChecked();
    await userEvent.click(consent); await userEvent.click(screen.getByRole("button", { name: "Find my stores" }));
    expect(input).toHaveValue("");
    const choice = await screen.findByRole("radio", { name: /My Etsy store/u });
    expect(choice).not.toBeChecked(); expect(screen.getByRole("radio", { name: /Other channel/u })).toBeDisabled();
    await userEvent.click(choice);
    act(() => { fixture.session.set(token("account-one", ["account"], { jti: "renewal" }), 3600); });
    expect(choice).toBeChecked(); expect(fetcher).toHaveBeenCalledTimes(2);
    expect((await axe.run(document.querySelector(".app-shell")!, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Connect store" }));
    await screen.findByRole("heading", { name: "Your store is ready." });
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Enter Mr. Lister" }));
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    expect(fixture.factory).toHaveBeenCalledWith({ shop_binding_id: ready.store!.shop_binding_id, expected_setup_version: 2 });
    expect(fixture.api.listJobs).toHaveBeenCalledOnce();
  });

  it("resumes a saved store after refresh without asking for another Printify token", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation((path) => Promise.resolve(response(path === "/v1/store-setup" ? { ...ready, state: "reconnect_required" } : ready)));
    const fixture = renderSetup(fetcher);
    await screen.findByRole("button", { name: "Finish connecting" });
    expect(screen.queryByLabelText("Connection token")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Finish connecting" }));
    await screen.findByRole("button", { name: "Enter Mr. Lister" });
    expect(fixture.renew).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(["/v1/store-setup", "/v1/connections/printify/activate"]);
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it("keeps a valid ready store locked while the capability is disabled", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(ready));
    const fixture = renderSetup(fetcher, { connectedWorkflow: false, groups: ["account", "seller"] });
    await screen.findByRole("button", { name: "Refresh setup" });
    expect(screen.queryByRole("button", { name: "Enter Mr. Lister" })).not.toBeInTheDocument();
    expect(fixture.factory).not.toHaveBeenCalled(); expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it.each(["supportEmail", "noticeVersion"] as const)("keeps a valid ready store locked without %s", async (missing) => {
    const accountConfig = { ...config }; delete accountConfig[missing];
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(ready));
    const fixture = renderSetup(fetcher, { groups: ["account", "seller"], accountConfig, route: "/" });
    await screen.findByRole("button", { name: "Refresh setup" });
    expect(screen.queryByRole("button", { name: "Enter Mr. Lister" })).not.toBeInTheDocument();
    expect(fixture.factory).not.toHaveBeenCalled(); expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it("retires a completed account callback without exchanging its one-use code again", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(ready));
    const fixture = renderSetup(fetcher, { groups: ["account", "seller"], route: "/auth/callback?code=consumed&state=consumed" });
    await screen.findByRole("button", { name: "Enter Mr. Lister" });
    expect(fixture.auth.completeSignIn).not.toHaveBeenCalled();
    expect(fixture.api.listJobs).not.toHaveBeenCalled();
  });

  it("preserves an open same-account workspace while its bearer token renews", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(ready));
    const fixture = renderSetup(fetcher, { groups: ["account", "seller"], route: "/" });
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    const renewal = deferred<{ accessToken: string; expiresInSeconds: number }>();
    fixture.session.setRenewer(() => renewal.promise);
    act(() => { fixture.session.set(token("account-one", ["account", "seller"]), 1, "memory-refresh"); });
    expect(screen.getByRole("heading", { name: "Let’s start with your artwork." })).toBeVisible();
    const pending = fixture.session.renewAccessToken();
    await act(async () => { renewal.resolve({ accessToken: token("account-one", ["account", "seller"], { jti: "new-bearer" }), expiresInSeconds: 3600 }); await pending; });
    expect(screen.getByRole("heading", { name: "Let’s start with your artwork." })).toBeVisible();
    expect(fixture.api.listJobs).toHaveBeenCalledOnce(); expect(fixture.factory).toHaveBeenCalledOnce();
  });

  it("shows a focused safe credential error and never reflects provider details", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation((path) => Promise.resolve(path === "/v1/store-setup" ? response(initial) : response({ error: { code: "CREDENTIAL_NOT_ACCEPTED", message: "raw-provider-token-do-not-display", request_id: "request-one" } }, 422)));
    renderSetup(fetcher); const input = await screen.findByLabelText("Connection token");
    await userEvent.type(input, "private-key"); await userEvent.click(screen.getByRole("checkbox", { name: /I’m authorized to connect/u })); await userEvent.click(screen.getByRole("button", { name: "Find my stores" }));
    expect(await screen.findByRole("alert")).toHaveFocus();
    expect(screen.getByRole("alert")).not.toHaveTextContent("raw-provider-token-do-not-display");
    expect(screen.getByLabelText("Connection token")).toHaveValue("");
    expect(screen.getByRole("alert")).toHaveTextContent("couldn’t read your stores");
    expect(screen.getByRole("alert")).not.toHaveTextContent("expired");
  });

  it("requires express authorization and links reviewable notices without sending a token", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(response(initial)));
    renderSetup(fetcher); const input = await screen.findByLabelText("Connection token");
    await userEvent.type(input, "not-authorized-yet");
    await userEvent.click(screen.getByRole("button", { name: "Find my stores" }));
    expect(fetcher).toHaveBeenCalledOnce();
    for (const kind of ["Privacy", "Terms"]) {
      const link = screen.getByRole("link", { name: `${kind} (opens in a new tab)` });
      expect(link).toHaveAttribute("href", `/${kind.toLowerCase()}`);
      expect(link).toHaveAttribute("target", "_blank"); expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
    await userEvent.click(screen.getByText("Where do I get my connection token?"));
    for (const scope of ["shops.read", "catalog.read", "products.read", "products.write", "uploads.read", "uploads.write", "print_providers.read"]) expect(screen.getByText(scope)).toBeVisible();
    expect(screen.getByText(/this check does not verify all publishing permissions/u)).toBeVisible();
    expect((await axe.run(document.querySelector(".app-shell")!, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("clears the token and merchant authorization when the signed-in account changes", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(response(initial)));
    const fixture = renderSetup(fetcher); const input = await screen.findByLabelText("Connection token");
    await userEvent.type(input, "private-token"); await userEvent.click(screen.getByRole("checkbox", { name: /I’m authorized to connect/u }));
    act(() => { fixture.session.set(token("another-account"), 3600); });
    expect(await screen.findByLabelText("Connection token")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: /I’m authorized to connect/u })).not.toBeChecked();
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(["/v1/store-setup", "/v1/store-setup"]);
    expect(input).toHaveValue("");
  });
});

function renderSetup(fetcher: typeof fetch, options: { connectedWorkflow?: boolean; groups?: string[]; route?: string; accountConfig?: AccountWorkspaceConfiguration } = {}) {
  const configuration = options.accountConfig ?? { ...config, connectedWorkflow: options.connectedWorkflow ?? true };
  const fixture = client(fetcher, { configuration, ...(options.groups === undefined ? {} : { groups: options.groups }) });
  const unused = () => vi.fn().mockRejectedValue(new Error("Unexpected workflow API call"));
  const api = { listJobs: vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "jobs", etag: null }), clearRecentJobs: unused(), getJob: unused(), getUpload: unused(), getReview: unused(), createUpload: unused(), authorizeUpload: unused(), completeUpload: unused(), cancelUpload: unused(), reviseListing: unused(), runAction: unused(), fetchArtwork: unused() } satisfies ApiPort;
  const auth = { session: fixture.session, startSignIn: vi.fn().mockResolvedValue(undefined), completeSignIn: vi.fn().mockResolvedValue("/"), signOut: vi.fn(() => { fixture.session.clear(); }) } satisfies AuthCoordinator;
  const factory = vi.fn().mockReturnValue(api);
  render(<MemoryRouter initialEntries={[options.route ?? "/store-setup"]}><AppRoutes dependencies={{ auth, api, accountConfig: configuration, accountApi: new BrowserAccountSetupClient(fixture.session, configuration, fetcher), storeConnectionAdapter: fixture.adapter, createAccountWorkflowApi: factory }} /></MemoryRouter>);
  return { ...fixture, api, factory, auth };
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((accept) => { resolve = accept; }); return { resolve, promise }; }
