import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../src/App";
import type { ApiPort } from "../src/api/client";
import { AuthError, MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { THEME_STORAGE_KEY } from "../src/theme";

afterEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("official public landing page", () => {
  it("keeps anonymous visitors on the public page without reading private work", () => {
    const { api, auth } = dependencies();
    renderApp(api, auth);

    expect(landingHeading()).toBeVisible();
    expect(document.title).toBe("Mr. Lister — Your next listing, made simpler.");
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "How it works" })).toHaveAttribute("href", "#how-it-works");
    expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u)).not.toBeInTheDocument();
    for (const method of Object.values(api)) expect(method).not.toHaveBeenCalled();
  });

  it("uses the existing fallback when a landing-page sign-in popup is blocked", async () => {
    const { api, auth } = dependencies({
      startPopupSignIn: vi.fn().mockRejectedValue(new AuthError("Your browser blocked the sign-in window.")),
    });
    renderApp(api, auth);
    await userEvent.click(within(screen.getByRole("main")).getByRole("button", { name: "Open seller workspace" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your browser blocked the sign-in window.");
    expect(auth.startPopupSignIn).toHaveBeenCalledWith("/");
    expect(landingHeading()).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue in this tab" }));
    expect(auth.startSignIn).toHaveBeenCalledWith("/");
    expect(api.listJobs).not.toHaveBeenCalled();
  });

  it.each([
    { label: "Light", value: "light", systemDark: true, resolved: "light" },
    { label: "Dark", value: "dark", systemDark: false, resolved: "dark" },
    { label: "Auto", value: "auto", systemDark: true, resolved: "dark" },
  ])("preserves $label through landing sign-in, upload home, and sign-out", async ({ label, value, systemDark, resolved }) => {
    const originalMatchMedia = window.matchMedia.bind(window);
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...originalMatchMedia(query),
      matches: query === "(prefers-color-scheme: dark)" && systemDark,
    }));
    const completed = deferred<string>();
    const { api, auth, session } = dependencies({ startPopupSignIn: vi.fn().mockReturnValue(completed.promise) });
    renderApp(api, auth);
    await userEvent.click(screen.getByRole("button", { name: /^Display theme:/u }));
    await userEvent.click(screen.getByRole("menuitemradio", { name: label }));
    expect(document.documentElement.dataset.theme).toBe(resolved);

    await userEvent.click(within(screen.getByRole("main")).getByRole("button", { name: "Open seller workspace" }));
    expect(auth.startPopupSignIn).toHaveBeenCalledWith("/");
    expect(screen.getByRole("dialog", { name: "Sign in to Mr. Lister" })).toBeVisible();
    await act(async () => {
      session.set("landing-access-token", 3600, "landing-refresh-token");
      completed.resolve("/");
      await completed.promise;
    });
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    await waitFor(() => expect(api.listJobs).toHaveBeenCalledOnce());
    expect(document.title).toBe("Uploads | Mr. Lister");
    expect(screen.queryByRole("heading", { level: 1, name: /Your artwork\.\s*Your next listing\s*\./u })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(screen.getByRole("button", { name: `Display theme: ${label}` })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(resolved);

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(auth.signOut).toHaveBeenCalledOnce();
    expect(landingHeading()).toBeVisible();
    expect(document.title).toBe("Mr. Lister — Your next listing, made simpler.");
    expect(screen.getByRole("button", { name: `Display theme: ${label}` })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(resolved);
    expect(Object.keys(window.localStorage)).toEqual([THEME_STORAGE_KEY]);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe(value);
    expect(window.sessionStorage.length).toBe(0);
    expect(api.listJobs).toHaveBeenCalledOnce();
  });

  it.each(["/jobs/job_private", "/uploads/upload_private"])("keeps %s protected instead of replacing it with marketing", async (route) => {
    const { api, auth } = dependencies();
    renderApp(api, auth, route);
    expect(screen.getByRole("heading", { name: "Restore your seller session." })).toBeVisible();
    expect(screen.queryByRole("heading", { level: 1, name: /Your artwork\.\s*Your next listing\s*\./u })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue securely" }));
    expect(auth.startPopupSignIn).toHaveBeenCalledWith(route);
    for (const method of Object.values(api)) expect(method).not.toHaveBeenCalled();
  });
});

function landingHeading() {
  return screen.getByRole("heading", { level: 1, name: /Your artwork\.\s*Your next listing\s*\./u });
}

function CurrentRoute() {
  return <p data-testid="route">{useLocation().pathname}</p>;
}

function renderApp(api: ApiPort, auth: AuthCoordinator, route = "/") {
  return render(<MemoryRouter initialEntries={[route]}><AppRoutes dependencies={{ api, auth }} /><CurrentRoute /></MemoryRouter>);
}

function dependencies(overrides: Partial<AuthCoordinator> = {}) {
  const session = new MemoryAuthSession();
  const unused = () => vi.fn().mockRejectedValue(new Error("Unexpected private API call"));
  const api = {
    listJobs: vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "landing-jobs", etag: null }),
    getJob: unused(), getUpload: unused(), getReview: unused(), createUpload: unused(), authorizeUpload: unused(),
    completeUpload: unused(), cancelUpload: unused(), reviseListing: unused(), runAction: unused(), fetchArtwork: unused(),
  } satisfies ApiPort;
  const auth = {
    session,
    startSignIn: vi.fn().mockResolvedValue(undefined),
    startPopupSignIn: vi.fn().mockReturnValue(new Promise<never>(() => undefined)),
    cancelPopupSignIn: vi.fn(),
    focusPopupSignIn: vi.fn(),
    completeSignIn: vi.fn().mockResolvedValue("/"),
    signOut: vi.fn(() => { session.clear(); }),
    ...overrides,
  } satisfies AuthCoordinator;
  return { api: vi.mocked(api), auth: vi.mocked(auth), session };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}
