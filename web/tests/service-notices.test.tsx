import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../src/App";
import type { ApiPort } from "../src/api/client";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { SERVICE_NOTICE_VERSION } from "../src/service-notices";

const issuer = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Sellers";
const routes = ["privacy", "terms"] as const;
describe("public service notices", () => {
  for (const mode of ["anonymous", "account", "owner", "judge"] as const) {
    it.each(routes)(`opens %s in ${mode} without loading private providers`, (kind) => {
      const fixture = renderNotice(kind, mode);
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(kind === "privacy" ? "Your privacy, in plain language." : "A few things to agree on.");
      expect(screen.getByText(`Version ${SERVICE_NOTICE_VERSION}`)).toBeVisible();
      expect(screen.getByText("Draft for operator review")).toBeVisible();
      expect(document.title).toBe(`${kind === "privacy" ? "Privacy" : "Terms"} | Mr. Lister`);
      for (const method of Object.values(fixture.api)) expect(method).not.toHaveBeenCalled();
      expect(fixture.getSetup).not.toHaveBeenCalled();
      expect(fixture.auth.startSignIn).not.toHaveBeenCalled();
      expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
      expect(screen.getAllByRole("link", { name: "Privacy" })[0]).toHaveAttribute("href", mode === "judge" ? "/judge/privacy" : "/privacy");
      expect(screen.getAllByRole("link", { name: "Terms" })[0]).toHaveAttribute("href", mode === "judge" ? "/judge/terms" : "/terms");
    });
  }

  it("displays only a configured public contact and otherwise leaves an explicit review placeholder", () => {
    const first = renderNotice("privacy", "anonymous");
    expect(screen.getByText(/Public contact details are being finalized/u)).toBeVisible();
    expect(document.querySelector('a[href^="mailto:"]')).toBeNull();
    first.unmount();
    renderNotice("privacy", "anonymous", "support@example.test");
    expect(screen.getByRole("link", { name: "support@example.test" })).toHaveAttribute("href", "mailto:support@example.test");
    expect(screen.getByText("Draft for operator review")).toBeVisible();
  });

  it("uses final notice presentation only with current operator review and valid contact", () => {
    const finalized = renderNotice("privacy", "anonymous", "support@example.com", SERVICE_NOTICE_VERSION);
    expect(screen.getByText("Current notice")).toBeVisible();
    expect(screen.queryByText("Draft for operator review")).not.toBeInTheDocument();
    finalized.unmount();
    const unreviewed = renderNotice("privacy", "anonymous", "support@example.com", "unreviewed-version");
    expect(screen.getByText("Draft for operator review")).toBeVisible();
    unreviewed.unmount();
    renderNotice("privacy", "anonymous", undefined, SERVICE_NOTICE_VERSION);
    expect(screen.getByText("Draft for operator review")).toBeVisible();
  });

  it.each(routes)("provides accessible %s disclosures", async (kind) => {
    const fixture = renderNotice(kind, "account");
    expect((await axe.run(fixture.container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
    if (kind === "privacy") {
      expect(screen.getByText(/stored in encrypted AWS Secrets Manager storage/u)).toBeVisible();
      expect(screen.getByText(/Gemma model hosted on AWS/u)).toBeVisible();
      expect(screen.getByText(/doesn’t request order or customer data/u)).toBeVisible();
      expect(screen.getByText(/Signing out ends your browser session but does not revoke/u)).toBeVisible();
    } else {
      expect(screen.getByText(/does not mean Printify has approved or endorsed/u)).toBeVisible();
      expect(screen.getByText(/You retain your rights in your artwork and products/u)).toBeVisible();
      expect(screen.getByText(/Printify does not provide support/u)).toBeVisible();
    }
  });
});

function renderNotice(kind: typeof routes[number], mode: "anonymous" | "account" | "owner" | "judge", supportEmail?: string, noticeVersion?: string) {
  const session = new MemoryAuthSession();
  if (mode !== "anonymous") session.set(`header.${btoa(JSON.stringify({ sub: "current-user", iss: issuer, client_id: "publicclient", token_use: "access", "cognito:groups": mode === "account" ? ["account"] : ["seller"] })).replace(/=/gu, "")}.signature`, 3600);
  const unused = () => vi.fn().mockRejectedValue(new Error("A public notice must never read private work"));
  const api = { listJobs: unused(), clearRecentJobs: unused(), getJob: unused(), getUpload: unused(), getReview: unused(), createUpload: unused(), authorizeUpload: unused(), completeUpload: unused(), cancelUpload: unused(), reviseListing: unused(), runAction: unused(), fetchArtwork: unused() } satisfies ApiPort;
  const getSetup = unused();
  const auth = { session, startSignIn: vi.fn().mockResolvedValue(undefined), completeSignIn: vi.fn().mockResolvedValue("/"), signOut: vi.fn() } satisfies AuthCoordinator;
  const basename = mode === "judge" ? "/judge" : "/";
  const result = render(<MemoryRouter basename={basename} initialEntries={[`${mode === "judge" ? "/judge" : ""}/${kind}`]}><AppRoutes dependencies={{ auth, api, accountApi: { getSetup }, accountConfig: { clientId: "publicclient", cognitoTokenUrl: "https://sellers.auth.us-west-2.amazoncognito.com/oauth2/token", issuer, selfServiceSignup: false, ...(supportEmail === undefined ? {} : { supportEmail }), ...(noticeVersion === undefined ? {} : { noticeVersion }) }, ...(mode === "judge" ? { judgeAccess: {} } : {}) }} /></MemoryRouter>);
  return { ...result, api, getSetup, auth };
}
