import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../src/App";
import { AppContext } from "../src/app-context";
import type { ApiPort } from "../src/api/client";
import { AuthError, MemoryAuthSession, relayPopupCallback, type AuthCoordinator } from "../src/auth/session";
import type * as AuthModule from "../src/auth/session";
import { SignInProvider, useSignIn } from "../src/auth/sign-in";
import { AuthCallbackPage } from "../src/pages/AuthCallbackPage";

vi.mock("../src/auth/session", async (importOriginal) => ({
  ...await importOriginal<typeof AuthModule>(),
  relayPopupCallback: vi.fn(),
}));

beforeEach(() => { vi.mocked(relayPopupCallback).mockReturnValue(false); });

describe("sign-in popup UI", () => {
  it("opens from the click, keeps the page visible, and lets the user bring the window forward", async () => {
    const auth = coordinator();
    renderFlow(auth);
    const trigger = screen.getByRole("button", { name: "Open sign-in" });
    fireEvent.click(trigger);
    expect(auth.startPopupSignIn).toHaveBeenCalledWith("/jobs/job_current");
    expect(auth.startSignIn).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Sign in to Mr. Lister" })).toBeVisible();
    expect(screen.getByText("Original login page")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Show sign-in window" }));
    expect(auth.focusPopupSignIn).toHaveBeenCalledOnce();
  });

  it("cancels and restores focus, ignoring a late popup result", async () => {
    const pending = deferred<string>();
    const auth = coordinator({ startPopupSignIn: vi.fn().mockReturnValue(pending.promise) });
    renderFlow(auth);
    const trigger = screen.getByRole("button", { name: "Open sign-in" });
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(auth.cancelPopupSignIn).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await act(async () => { pending.resolve("/jobs/job_current"); await pending.promise; });
    expect(screen.getByTestId("route")).toHaveTextContent(/^\/$/u);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("handles the native Escape dismissal and cancels an unfinished popup on unmount", async () => {
    const auth = coordinator();
    const rendered = renderFlow(auth);
    await userEvent.click(screen.getByRole("button", { name: "Open sign-in" }));
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(auth.cancelPopupSignIn).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "Open sign-in" }));
    rendered.unmount();
    expect(auth.cancelPopupSignIn).toHaveBeenCalledTimes(2);
  });

  it("shows safe blocked-window feedback and offers the existing full-tab flow with the exact return route", async () => {
    const redirect = deferred<void>();
    const auth = coordinator({
      startPopupSignIn: vi.fn().mockRejectedValue(new AuthError("Your browser blocked the sign-in window.")),
      startSignIn: vi.fn().mockReturnValue(redirect.promise),
    });
    renderFlow(auth);
    await userEvent.click(screen.getByRole("button", { name: "Open sign-in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your browser blocked the sign-in window.");
    expect(screen.getByRole("heading", { name: "Sign-in needs another try" })).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Continue in this tab" }));
    expect(auth.startSignIn).toHaveBeenCalledWith("/jobs/job_current");
    expect(screen.getByRole("status")).toHaveTextContent("Opening secure sign-in");
    await act(async () => { redirect.resolve(); await redirect.promise; });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("retries in a popup and navigates the original page after a successful handoff", async () => {
    const completed = deferred<string>();
    const auth = coordinator({ startPopupSignIn: vi.fn()
      .mockRejectedValueOnce(new AuthError("The sign-in window was closed."))
      .mockReturnValueOnce(completed.promise) });
    renderFlow(auth);
    await userEvent.click(screen.getByRole("button", { name: "Open sign-in" }));
    await userEvent.click(await screen.findByRole("button", { name: "Try popup again" }));
    expect(auth.startPopupSignIn).toHaveBeenCalledTimes(2);
    await act(async () => { completed.resolve("/jobs/job_current"); await completed.promise; });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_current");
  });

  it("does not display unexpected provider error details and catches fallback failures", async () => {
    const auth = coordinator({
      startPopupSignIn: vi.fn().mockRejectedValue(new Error("private provider details")),
      startSignIn: vi.fn().mockRejectedValue(new Error("private redirect details")),
    });
    renderFlow(auth);
    await userEvent.click(screen.getByRole("button", { name: "Open sign-in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in could not be completed. Please try again.");
    await userEvent.click(screen.getByRole("button", { name: "Continue in this tab" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in could not be completed. Please try again.");
    expect(screen.queryByText(/private/u)).not.toBeInTheDocument();
  });

  it.each([
    { route: "/", label: "Open seller workspace", landmark: "main" },
    { route: "/", label: "Open seller workspace", landmark: "contentinfo" },
    { route: "/", label: "Sign in", landmark: "banner" },
    { route: "/jobs/job_current", label: "Continue securely", landmark: "main" },
  ])("wires $landmark $label to the shared popup flow", async ({ route, label, landmark }) => {
    const auth = coordinator();
    render(<MemoryRouter initialEntries={[route]}><AppRoutes dependencies={{ api: api(), auth }} /></MemoryRouter>);
    await userEvent.click(within(screen.getByRole(landmark)).getByRole("button", { name: label }));
    expect(auth.startPopupSignIn).toHaveBeenCalledWith(route);
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("keeps standalone fixtures compatible with a coordinator that only redirects", async () => {
    const auth = coordinator();
    render(<MemoryRouter><AppContext.Provider value={{ api: api(), auth }}><Trigger /></AppContext.Provider></MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Open sign-in" }));
    expect(auth.startSignIn).toHaveBeenCalledWith("/jobs/job_current");
    expect(auth.startPopupSignIn).not.toHaveBeenCalled();
  });
});

describe("sign-in callback routing", () => {
  it("relays an owned popup callback without exchanging tokens in the popup", () => {
    vi.mocked(relayPopupCallback).mockReturnValue(true);
    const auth = coordinator();
    renderCallback(auth);
    expect(relayPopupCallback).toHaveBeenCalledWith("?code=one-use-code&state=expected-state", expect.any(Function));
    expect(auth.completeSignIn).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Exchanging the one-use authorization code.");
  });

  it("shows safe lost-opener feedback and supports a full-page retry", async () => {
    vi.mocked(relayPopupCallback).mockImplementation(() => { throw new AuthError("The original sign-in page is no longer available."); });
    const auth = coordinator();
    renderCallback(auth);
    expect(await screen.findByRole("alert")).toHaveTextContent("The original sign-in page is no longer available.");
    expect(auth.completeSignIn).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Try sign-in again" }));
    expect(auth.startSignIn).toHaveBeenCalledWith("/");
  });

  it("still completes normal full-page sign-in", async () => {
    const auth = coordinator({ completeSignIn: vi.fn().mockResolvedValue("/jobs/job_current") });
    renderCallback(auth);
    await waitFor(() => expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_current"));
    expect(auth.completeSignIn).toHaveBeenCalledWith("?code=one-use-code&state=expected-state");
  });

  it("keeps a full-page callback alive through StrictMode effect cleanup", async () => {
    const exchange = deferred<string>();
    const auth = coordinator({ completeSignIn: vi.fn().mockReturnValue(exchange.promise) });
    render(<StrictMode><MemoryRouter initialEntries={["/auth/callback?code=one-use-code&state=expected-state"]}><AppRoutes dependencies={{ api: api(), auth }} /><CurrentRoute /></MemoryRouter></StrictMode>);
    expect(auth.completeSignIn).toHaveBeenCalledOnce();
    expect(auth.cancelPopupSignIn).not.toHaveBeenCalled();
    await act(async () => { exchange.resolve("/jobs/job_current"); await exchange.promise; });
    expect(screen.getByTestId("route")).toHaveTextContent("/jobs/job_current");
    expect(auth.cancelPopupSignIn).not.toHaveBeenCalled();
  });

  it("offers a retry if the opener does not acknowledge a popup handoff", async () => {
    let reportFailure: ((reason: AuthError) => void) | undefined;
    vi.mocked(relayPopupCallback).mockImplementation((_search, onFailure) => {
      reportFailure = onFailure;
      return true;
    });
    renderCallback(coordinator());
    await act(async () => {
      reportFailure?.(new AuthError("The original sign-in page did not respond. Please try signing in again."));
      await Promise.resolve();
    });
    expect(screen.getByRole("alert")).toHaveTextContent("The original sign-in page did not respond.");
    expect(screen.getByRole("button", { name: "Try sign-in again" })).toBeVisible();
  });
});

function Trigger() {
  const { startSignIn } = useSignIn();
  return <><p>Original login page</p><button type="button" onClick={() => { startSignIn("/jobs/job_current"); }}>Open sign-in</button></>;
}

function CurrentRoute() {
  return <p data-testid="route">{useLocation().pathname}</p>;
}

function renderFlow(auth: AuthCoordinator) {
  return render(<MemoryRouter><AppContext.Provider value={{ api: api(), auth }}><SignInProvider><Trigger /><CurrentRoute /></SignInProvider></AppContext.Provider></MemoryRouter>);
}

function renderCallback(auth: AuthCoordinator) {
  return render(<MemoryRouter initialEntries={["/auth/callback?code=one-use-code&state=expected-state"]}><AppContext.Provider value={{ api: api(), auth }}><AuthCallbackPage /><CurrentRoute /></AppContext.Provider></MemoryRouter>);
}

function coordinator(overrides: Partial<AuthCoordinator> = {}) {
  return vi.mocked({
    session: new MemoryAuthSession(),
    startSignIn: vi.fn().mockResolvedValue(undefined),
    startPopupSignIn: vi.fn().mockReturnValue(new Promise<never>(() => undefined)),
    cancelPopupSignIn: vi.fn(),
    focusPopupSignIn: vi.fn(),
    completeSignIn: vi.fn().mockResolvedValue("/"),
    signOut: vi.fn(),
    ...overrides,
  });
}

function api(): ApiPort {
  const unused = () => Promise.reject(new Error("Unexpected API call"));
  return {
    listJobs: unused, getJob: unused, getUpload: unused, getReview: unused,
    createUpload: unused, authorizeUpload: unused, completeUpload: unused,
    cancelUpload: unused, reviseListing: unused, runAction: unused, fetchArtwork: unused,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => { resolve = resolvePromise; reject = rejectPromise; });
  return { promise, resolve, reject };
}
