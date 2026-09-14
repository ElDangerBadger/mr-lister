import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountApplication } from "../src/bootstrap";
import { captureJudgeInvitation, JUDGE_RESTORE_BLOCK_KEY } from "../src/auth/judge-session";
import type { RuntimeConfig } from "../src/contracts";

const invitation = "z".repeat(43);
const config: RuntimeConfig = {
  cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
  cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
  cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
  client_id: "primaryclient", redirect_uri: `${window.location.origin}/judge/auth/callback`,
  scopes: ["openid", "mr-lister-api/seller"],
  judge_access: { identity_provider: "MrListerJudge", upstream_logout_url: "https://judge.auth.us-west-2.amazoncognito.com/logout", upstream_client_id: "judgeclient", session_entry: true, cleanup_after_minutes: 30 },
};
let root: Root | undefined;
let container: HTMLDivElement | undefined;
afterEach(() => {
  act(() => { root?.unmount(); });
  container?.remove(); root = undefined; container = undefined;
  vi.restoreAllMocks(); vi.unstubAllGlobals(); window.localStorage.removeItem(JUDGE_RESTORE_BLOCK_KEY);
  window.history.replaceState(null, "", "/");
});
function tokens() { return new Response(JSON.stringify({ access_token: "memory-judge-token", expires_in: 3600, token_type: "Bearer" })); }
async function mount(path = `/judge/#access=${invitation}`, runtime = config) {
  window.history.replaceState(null, "", path);
  const captured = captureJudgeInvitation();
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  await act(async () => { mountApplication(root!, runtime, captured); await Promise.resolve(); });
}
function endpoints() {
  const requests: string[] = [];
  const fetcher = vi.fn<typeof fetch>((input) => {
    const url = input instanceof Request ? input.url : input.toString();
    requests.push(url);
    if (url === "/v1/judge-session/redeem" || url === "/v1/judge-session/refresh") return Promise.resolve(tokens());
    if (url === "/v1/judge-session/logout") return Promise.resolve(new Response(null, { status: 204 }));
    if (url.startsWith("/v1/jobs?")) return Promise.resolve(new Response(JSON.stringify({ jobs: [], next_cursor: null }), { headers: { "Content-Type": "application/json", "X-Request-Id": "judge-session-test" } }));
    return Promise.reject(new Error("Unexpected endpoint"));
  });
  vi.stubGlobal("fetch", fetcher);
  return { fetcher, requests };
}

describe("credential-free judge entry in the actual application", () => {
  it("waits for explicit entry without a modal, opens the workspace, and signs out in judge mode", async () => {
    const api = endpoints();
    const popup = vi.spyOn(window, "open");
    await mount();
    expect(window.location.hash).toBe("");
    expect(api.requests).toEqual([]);
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Enter judge workspace" }));
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    await waitFor(() => { expect(api.requests.some((path) => path.startsWith("/v1/jobs?"))).toBe(true); });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(popup).not.toHaveBeenCalled();
    expect(screen.getByText("Demo listings are scheduled for removal 30 minutes after live publication.")).toBeVisible();
    expect(api.requests.filter((path) => path === "/v1/judge-session/redeem")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByText("You’re signed out. Reopen your private access link to enter again.");
    expect(window.location.pathname).toBe("/judge");
    expect(api.requests).toContain("/v1/judge-session/logout");
    expect(api.requests.some((path) => path.includes("cognito"))).toBe(false);
  });
  it("restores a cookie on reload without an invitation or credentials", async () => {
    const api = endpoints();
    await mount("/judge/");
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    expect(api.requests.filter((path) => path === "/v1/judge-session/refresh")).toHaveLength(1);
    expect(api.requests).not.toContain("/v1/judge-session/redeem");
  });
  it("does not promise timed removal before cleanup is enabled", async () => {
    endpoints();
    const { cleanup_after_minutes: ignored, ...judgeAccess } = config.judge_access!;
    void ignored;
    await mount(`/judge/#access=${invitation}`, { ...config, judge_access: judgeAccess });
    expect(screen.queryByText(/scheduled for removal/)).not.toBeInTheDocument();
    expect(screen.getByText(/Publishing creates a real Etsy listing/)).toBeVisible();
  });
  it("shows a helpful private-link message for an anonymous reload", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 }));
    vi.stubGlobal("fetch", fetcher);
    await mount("/judge/");
    await screen.findByText("Open the private judge access link provided to you to enter this workspace.");
    expect(screen.queryByRole("button", { name: "Enter judge workspace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("cancel keeps a late redemption from authenticating or navigating", async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>((resolve) => { finish = resolve; })));
    await mount();
    await userEvent.click(screen.getByRole("button", { name: "Enter judge workspace" }));
    expect(screen.getByRole("button", { name: "Opening workspace…" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await act(async () => { finish(tokens()); await Promise.resolve(); });
    expect(screen.getByRole("heading", { name: "Try the full listing journey." })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => { root!.unmount(); });
    container!.remove();
    const reloadFetch = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", reloadFetch);
    await mount("/judge/");
    expect(reloadFetch).not.toHaveBeenCalled();
    expect(screen.getByText("Open the private judge access link provided to you to enter this workspace.")).toBeVisible();
  });
});
