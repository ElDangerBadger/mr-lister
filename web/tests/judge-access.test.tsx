import axe from "axe-core";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import type { AppDependencies } from "../src/app-context";
import type { ApiPort } from "../src/api/client";
import { AuthError, MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { THEME_STORAGE_KEY } from "../src/theme";

afterEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("judge access in the existing application", () => {
  it("introduces the live workflow without authenticating or reading private work", () => {
    const setup = dependencies();
    renderJudge(setup);

    expect(screen.getByRole("heading", { level: 1, name: "Try the full listing journey." })).toBeVisible();
    expect(document.title).toBe("Judge access | Mr. Lister");
    expect(screen.getByRole("complementary", { name: "Judge walkthrough" })).toHaveTextContent(
      "Publishing creates a real Etsy listing after your final confirmation.",
    );
    const journey = screen.getByRole("list", { name: "The listing journey" });
    expect(within(journey).getAllByRole("heading").map((heading) => heading.textContent)).toEqual(["Upload", "Review", "Publish"]);
    expect(screen.getByRole("link", { name: "Mr. Lister seller review home" })).toHaveAttribute("href", "/judge");
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main-content");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByRole("button", { name: /Google|Apple|Facebook/iu })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review prepared example" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u)).not.toBeInTheDocument();
    expect(setup.session.getStatus()).toBe("anonymous");
    expect(setup.auth.startSignIn).not.toHaveBeenCalled();
    expect(setup.auth.startPopupSignIn).not.toHaveBeenCalled();
    for (const method of Object.values(setup.api)) expect(method).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("provides the bundled sample as a download without uploading it", () => {
    const setup = dependencies();
    renderJudge(setup);
    const artwork = screen.getByRole("img", { name: "Sample artwork: a teal wave and mountain beneath an orange sunset" });
    const download = screen.getByRole("link", { name: "Download sample artwork" });
    expect(download).toHaveAttribute("href", artwork.getAttribute("src"));
    expect(download).toHaveAttribute("download", "mr-lister-sample-artwork.png");
    expect(download.getAttribute("href")).toMatch(/^\/(?!judge\/).*judge-sample-artwork\.png$/u);
    expect(download).not.toHaveAttribute("target");
    expect(setup.api.createUpload).not.toHaveBeenCalled();
    expect(setup.api.runAction).not.toHaveBeenCalled();
  });

  it("uses existing secure sign-in and preserves the popup-blocked fallback", async () => {
    const setup = dependencies();
    setup.auth.startPopupSignIn.mockRejectedValue(new AuthError("Your browser blocked the sign-in window."));
    renderJudge(setup);
    await userEvent.click(screen.getByRole("button", { name: "Sign in with judge access" }));
    expect(setup.auth.startPopupSignIn).toHaveBeenCalledWith("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("Your browser blocked the sign-in window.");
    await userEvent.click(screen.getByRole("button", { name: "Continue in this tab" }));
    expect(setup.auth.startSignIn).toHaveBeenCalledWith("/");
    expect(setup.session.getStatus()).toBe("anonymous");
    for (const method of Object.values(setup.api)) expect(method).not.toHaveBeenCalled();
  });

  it("opens the configured prepared example only through existing sign-in", async () => {
    const setup = dependencies("job_prepared_example");
    renderJudge(setup);
    await userEvent.click(screen.getByRole("button", { name: "Review prepared example" }));
    expect(setup.auth.startPopupSignIn).toHaveBeenCalledExactlyOnceWith("/jobs/job_prepared_example");
    expect(setup.session.getStatus()).toBe("anonymous");
    expect(window.location.pathname).toBe("/judge/");
    for (const method of Object.values(setup.api)) expect(method).not.toHaveBeenCalled();
  });

  it.each(["/jobs/job_private", "/uploads/upload_private"])("keeps judge deep link %s protected", async (route) => {
    const setup = dependencies();
    renderJudge(setup, `/judge${route}`);
    expect(screen.getByRole("heading", { name: "Restore your seller session." })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "Judge walkthrough" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Continue securely" }));
    expect(setup.auth.startPopupSignIn).toHaveBeenCalledWith(route);
    expect(setup.session.getStatus()).toBe("anonymous");
    for (const method of Object.values(setup.api)) expect(method).not.toHaveBeenCalled();
  });

  it("keeps the approved upload workspace and routes prepared resources beneath /judge", async () => {
    const setup = dependencies("job_prepared_example");
    setup.session.set("test-session", 3600);
    renderJudge(setup);
    expect(screen.getByRole("heading", { name: "Let’s start with your artwork." })).toBeVisible();
    await waitFor(() => expect(setup.api.listJobs).toHaveBeenCalledExactlyOnceWith());
    const resources = screen.getByRole("navigation", { name: "Judge resources" });
    expect(within(resources).getByRole("link", { name: "Review prepared example" })).toHaveAttribute("href", "/judge/jobs/job_prepared_example");
    expect(within(resources).getByRole("link", { name: "Download sample artwork" })).toHaveAttribute("download", "mr-lister-sample-artwork.png");
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", "/judge");
    expect(screen.queryByRole("button", { name: "Submit" })).not.toBeInTheDocument();
    expect(setup.api.createUpload).not.toHaveBeenCalled();
    expect(setup.api.runAction).not.toHaveBeenCalled();
    expect(setup.auth.startPopupSignIn).not.toHaveBeenCalled();
  });

  it("omits the prepared-example shortcut when no job is configured", async () => {
    const setup = dependencies();
    setup.session.set("test-session", 3600);
    renderJudge(setup);
    await waitFor(() => expect(setup.api.listJobs).toHaveBeenCalledOnce());
    expect(screen.queryByRole("link", { name: "Review prepared example" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download sample artwork" })).toBeVisible();
  });

  it.each([
    { label: "Light", value: "light", systemDark: true, resolved: "light" },
    { label: "Dark", value: "dark", systemDark: false, resolved: "dark" },
    { label: "Auto", value: "auto", systemDark: true, resolved: "dark" },
  ])("preserves $label through judge sign-in and sign-out without storing judge authority", async ({ label, value, systemDark, resolved }) => {
    const originalMatchMedia = window.matchMedia.bind(window);
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...originalMatchMedia(query),
      matches: query === "(prefers-color-scheme: dark)" && systemDark,
    }));
    const setup = dependencies();
    const completed = deferred<string>();
    setup.auth.startPopupSignIn.mockReturnValue(completed.promise);
    renderJudge(setup);
    await userEvent.click(screen.getByRole("button", { name: /^Display theme:/u }));
    await userEvent.click(screen.getByRole("menuitemradio", { name: label }));
    expect(document.documentElement.dataset.theme).toBe(resolved);
    await userEvent.click(screen.getByRole("button", { name: "Sign in with judge access" }));
    await act(async () => {
      setup.session.set("test-session", 3600);
      completed.resolve("/");
      await completed.promise;
    });
    await screen.findByRole("heading", { name: "Let’s start with your artwork." });
    expect(window.location.pathname).toBe("/judge");
    expect(screen.getByRole("button", { name: `Display theme: ${label}` })).toBeVisible();
    expect(document.documentElement.dataset.theme).toBe(resolved);
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(setup.auth.signOut).toHaveBeenCalledOnce();
    expect(screen.getByRole("heading", { name: "Try the full listing journey." })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "Judge walkthrough" })).toBeVisible();
    expect(screen.queryByRole("navigation", { name: "Judge resources" })).not.toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(resolved);
    expect(Object.keys(window.localStorage)).toEqual([THEME_STORAGE_KEY]);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe(value);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("leaves the normal owner landing unchanged when judge config is absent", () => {
    const { api, auth } = dependencies();
    render(<App dependencies={{ api, auth }} />);
    expect(screen.getByRole("heading", { level: 1, name: /Your artwork\.\s*Your next listing\s*\./u })).toBeVisible();
    expect(screen.queryByRole("complementary", { name: "Judge walkthrough" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Download sample artwork" })).not.toBeInTheDocument();
    for (const method of Object.values(api)) expect(method).not.toHaveBeenCalled();
  });

  it("has accessible landmarks, headings and controls with a configured example", async () => {
    const { container } = renderJudge(dependencies("job_prepared_example"));
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});

function renderJudge(setup: AppDependencies, route = "/judge/") {
  window.history.replaceState(null, "", route);
  return render(<App dependencies={setup} />);
}

function dependencies(preparedJobId?: string) {
  const session = new MemoryAuthSession();
  const unused = () => vi.fn().mockRejectedValue(new Error("Unexpected private API call"));
  const api = {
    listJobs: vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "judge-jobs", etag: null }),
    getJob: unused(), getUpload: unused(), getReview: unused(), createUpload: unused(), authorizeUpload: unused(),
    completeUpload: unused(), cancelUpload: unused(), reviseListing: unused(), runAction: unused(), fetchArtwork: unused(),
  } satisfies ApiPort;
  const auth = {
    session,
    startSignIn: vi.fn().mockResolvedValue(undefined),
    startPopupSignIn: vi.fn().mockReturnValue(new Promise<never>(() => undefined)),
    cancelPopupSignIn: vi.fn(), focusPopupSignIn: vi.fn(),
    completeSignIn: vi.fn().mockResolvedValue("/"),
    signOut: vi.fn(() => { session.clear(); }),
  } satisfies AuthCoordinator;
  return { api, auth, session, judgeAccess: preparedJobId === undefined ? {} : { preparedJobId } } satisfies AppDependencies & { session: MemoryAuthSession };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}
