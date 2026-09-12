import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { WorkspaceLink, WorkspaceNavigationProvider, useNavigationProtection, type NavigationProtectionReason } from "../src/navigation/WorkspaceNavigation";

describe("workspace navigation protection", () => {
  it("keeps ordinary navigation and router state when the review is clean", async () => {
    renderWorkspace("none");
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/?from=review#uploads");
    expect(screen.getByTestId("navigation-state")).toHaveTextContent("dashboard");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("preserves edits on Stay here and requires an explicit unsaved departure", async () => {
    renderWorkspace("unsaved");
    const title = screen.getByRole("textbox", { name: "Listing title" });
    await userEvent.type(title, "My revised title");
    const dashboard = screen.getByRole("link", { name: "Dashboard" });
    await userEvent.click(dashboard);
    expect(screen.getByRole("dialog", { name: "Leave your unsaved changes?" })).toBeVisible();
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/jobs\/one$/u);
    await userEvent.click(screen.getByRole("button", { name: "Stay here" }));
    expect(title).toHaveValue("My revised title");
    expect(dashboard).toHaveFocus();
    await userEvent.click(dashboard);
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/?from=review#uploads");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("traps keyboard focus and dismisses with Escape or native dialog cancel", async () => {
    renderWorkspace("unsaved");
    const dashboard = screen.getByRole("link", { name: "Dashboard" });
    await userEvent.click(dashboard);
    const stay = screen.getByRole("button", { name: "Stay here" });
    const leave = screen.getByRole("button", { name: "Leave without saving" });
    expect(stay).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(leave).toHaveFocus();
    await userEvent.tab();
    expect(stay).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(dashboard).toHaveFocus();
    await userEvent.click(dashboard);
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(dashboard).toHaveFocus();
  });

  it("restores the clicked link when the browser leaves focus in the editor", async () => {
    renderWorkspace("unsaved");
    const title = screen.getByRole("textbox", { name: "Listing title" });
    const dashboard = screen.getByRole("link", { name: "Dashboard" });
    title.focus();
    fireEvent.change(title, { target: { value: "Keep this revision" } });
    expect(title).toHaveFocus();
    // Safari does not necessarily focus an anchor when it is clicked.
    await act(async () => {
      fireEvent.click(dashboard);
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Stay here" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(dashboard).toHaveFocus();
    expect(title).toHaveValue("Keep this revision");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/jobs\/one$/u);
  });

  it.each([
    { reason: "saving" as const, title: "Your changes are saving" },
    { reason: "reconciling" as const, title: "Your listing is updating" },
  ])("blocks departure during $reason and never queues an automatic jump", async ({ reason, title }) => {
    renderWorkspace(reason);
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByRole("dialog", { name: title })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Leave without saving" })).not.toBeInTheDocument();
    const stay = screen.getByRole("button", { name: "Stay here" });
    await userEvent.tab();
    expect(stay).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Finish saving" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/jobs\/one$/u);
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/?from=review#uploads");
  });

  it("warns on tab close or reload only while the current review is protected", async () => {
    renderWorkspace("unsaved");
    const guarded = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(guarded);
    expect(guarded.defaultPrevented).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Finish saving" }));
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
  });

  it("clears pending navigation and protection when another route replaces the job", async () => {
    renderWorkspace("unsaved");
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Open another job externally" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/jobs/two");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/?from=review#uploads");
  });

  it("keeps modified, new-tab, and download clicks outside the discard guard", () => {
    renderWorkspace("unsaved");
    for (const [name, options] of [
      ["Dashboard", { ctrlKey: true }],
      ["Dashboard", { metaKey: true }],
      ["Dashboard", { shiftKey: true }],
      ["Dashboard", { altKey: true }],
      ["Open in new tab", {}],
      ["Download artwork", {}],
    ] as const) {
      const preventedByLink = vi.fn();
      document.addEventListener("click", (event) => {
        preventedByLink(event.defaultPrevented);
        event.preventDefault();
      }, { once: true });
      fireEvent.click(screen.getByRole("link", { name }), options);
      expect(preventedByLink).toHaveBeenCalledWith(false);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByTestId("location")).toHaveTextContent(/^\/jobs\/one$/u);
    }
  });

  it("respects caller cancellation and avoids a discard prompt for the current page", async () => {
    renderWorkspace("unsaved");
    await userEvent.click(screen.getByRole("link", { name: "Canceled link" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Current listing" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/jobs\/one$/u);
  });

  it("supports standalone fixtures without a navigation provider", async () => {
    render(<MemoryRouter initialEntries={["/jobs/one"]}><Workspace initialReason="unsaved" /></MemoryRouter>);
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/?from=review#uploads");
  });
});

function renderWorkspace(reason: NavigationProtectionReason) {
  return render(<StrictMode><MemoryRouter initialEntries={["/jobs/one"]}><WorkspaceNavigationProvider>
    <Workspace initialReason={reason} />
  </WorkspaceNavigationProvider></MemoryRouter></StrictMode>);
}

function Workspace({ initialReason }: { initialReason: NavigationProtectionReason }) {
  const location = useLocation();
  const navigate = useNavigate();
  return <>
    <p data-testid="location">{location.pathname}{location.search}{location.hash}</p>
    <p data-testid="navigation-state">{JSON.stringify(location.state)}</p>
    <WorkspaceLink to="/?from=review#uploads" state={{ from: "dashboard" }}>Dashboard</WorkspaceLink>
    <WorkspaceLink to="/" target="_blank">Open in new tab</WorkspaceLink>
    <WorkspaceLink to="/artwork.png" download>Download artwork</WorkspaceLink>
    <WorkspaceLink to="/" onClick={(event) => { event.preventDefault(); }}>Canceled link</WorkspaceLink>
    <WorkspaceLink to={location.pathname}>Current listing</WorkspaceLink>
    <button type="button" onClick={() => { void navigate("/jobs/two"); }}>Open another job externally</button>
    {location.pathname === "/jobs/one" && <Review initialReason={initialReason} />}
  </>;
}

function Review({ initialReason }: { initialReason: NavigationProtectionReason }) {
  const [reason, setReason] = useState(initialReason);
  useNavigationProtection(reason);
  return <>
    <input aria-label="Listing title" />
    <button type="button" onClick={() => { setReason("none"); }}>Finish saving</button>
  </>;
}
