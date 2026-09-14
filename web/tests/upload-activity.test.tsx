import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AppContext, type AppDependencies } from "../src/app-context";
import { MemoryAuthSession } from "../src/auth/session";
import { HomePage } from "../src/pages/HomePage";
import type { BatchUploadItemPhase, BatchUploadItemState, useUpload } from "../src/upload/upload-context";

const upload = vi.hoisted((): Pick<ReturnType<typeof useUpload>, "state" | "batch" | "reset"> => ({
  state: { phase: "idle", uploadId: null, jobId: null, filename: null, progress: 0, message: "", requestId: null },
  batch: { phase: "idle", items: [], message: "" }, reset: vi.fn(),
}));
vi.mock("../src/upload/upload-context", async (importOriginal) => ({
  ...await importOriginal<Record<string, unknown>>(), useUpload: () => upload,
}));

describe("active upload feedback", () => {
  it.each(["validating", "hashing", "creating_intent", "uploading", "finalizing"] as const)(
    "shows accessible activity during %s and stops it when that upload fails", (phase) => {
      setItem(phase);
      const dependencies = appDependencies();
      const { container, rerender } = render(<MemoryRouter><AppContext.Provider value={dependencies}><HomePage /></AppContext.Provider></MemoryRouter>);
      const activity = container.querySelector(".queue-status .activity-status");
      expect(activity).toHaveAttribute("role", "status");
      expect(activity).toHaveAttribute("aria-live", "polite");
      expect(activity).toHaveTextContent(/Checking artwork|Starting upload|Uploading artwork|Verifying artwork/u);
      expect(activity?.querySelector(".activity-status-dot")).toHaveAttribute("aria-hidden", "true");
      if (phase === "uploading") {
        expect(screen.getByRole("progressbar", { name: "art.png upload progress" })).toHaveAttribute("value", "42");
      } else {
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
      }

      setItem("error");
      rerender(<MemoryRouter><AppContext.Provider value={dependencies}><HomePage /></AppContext.Provider></MemoryRouter>);
      expect(container.querySelector(".activity-status")).toBeNull();
      expect(screen.getByText("Upload needs attention")).toBeVisible();
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    },
  );

  it.each(["queued", "complete", "expired"] as const)("does not imply upload activity for %s", (phase) => {
    setItem(phase);
    const { container } = render(<MemoryRouter><AppContext.Provider value={appDependencies()}><HomePage /></AppContext.Provider></MemoryRouter>);
    expect(container.querySelector(".activity-status")).toBeNull();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});

function setItem(phase: BatchUploadItemPhase) {
  const item: BatchUploadItemState = {
    id: "one", position: 1, filename: "art.png", preparedFilename: "art.png", sizeBytes: 10,
    sourceFormat: "png", phase, progress: 42, uploadId: "upload_one", jobId: "job_one",
    message: "Current upload detail", error: phase === "error" ? "Upload failed" : null, requestId: null,
  };
  upload.batch = { phase: ["complete", "expired", "error"].includes(phase) ? "complete" : "running", items: [item], message: "One artwork file" };
}

function appDependencies(): AppDependencies {
  const never = () => new Promise<never>(() => undefined);
  const session = new MemoryAuthSession();
  session.set("access", 3600, "refresh");
  return {
    api: { listJobs: never, getJob: never, getUpload: never, getReview: never, createUpload: never, authorizeUpload: never, completeUpload: never, cancelUpload: never, reviseListing: never, runAction: never, fetchArtwork: never },
    auth: { session, startSignIn: never, completeSignIn: never, signOut: vi.fn() },
  };
}
