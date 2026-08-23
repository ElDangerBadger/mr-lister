import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import type { ApiPort } from "../src/api/client";
import { AppRoutes } from "../src/App";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { uploadRecoverySchema } from "../src/contracts";

describe("upload route authority", () => {
  it("ignores a delayed upload A completion after recovery has moved to upload B", async () => {
    const first = uploadRecoverySchema.parse({
      ...browserFixtures.upload_recovery,
      upload_id: "upload_A",
      job_id: "job_A",
      authorization_expires_at: null,
    });
    const second = uploadRecoverySchema.parse({
      ...first,
      upload_id: "upload_B",
      job_id: "job_B",
      filename: "second.png",
    });
    let resolveFirstCompletion: ((value: ReturnType<typeof completedUploadResponse>) => void) | undefined;
    const firstCompletion = new Promise<ReturnType<typeof completedUploadResponse>>((resolve) => { resolveFirstCompletion = resolve; });
    const unresolved = new Promise<never>(() => undefined);
    const getUpload = vi.fn().mockImplementation((uploadId: string) => Promise.resolve({
      value: uploadId === first.upload_id ? first : second,
      requestId: uploadId === first.upload_id ? "request-A" : "request-B",
      etag: null,
    }));
    const completeUpload = vi.fn().mockImplementation((uploadId: string) => uploadId === first.upload_id ? firstCompletion : unresolved);
    const session = new MemoryAuthSession();
    session.set("access", 3600, "refresh");
    const never = () => Promise.reject(new Error("Unexpected test call"));
    const api: ApiPort = {
      listJobs: never,
      getJob: never,
      getUpload,
      getReview: never,
      createUpload: never,
      authorizeUpload: never,
      completeUpload,
      cancelUpload: never,
      reviseListing: never,
      runAction: never,
      fetchArtwork: never,
    };
    const auth: AuthCoordinator = { session, startSignIn: never, completeSignIn: never, signOut: vi.fn() };
    render(
      <MemoryRouter initialEntries={[`/uploads/${first.upload_id}`]}>
        <NavigateToUploadB />
        <AppRoutes dependencies={{ api, auth }} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await waitFor(() => expect(completeUpload).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Open upload B" }));
    await waitFor(() => expect(getUpload).toHaveBeenCalledWith("upload_B"));
    await waitFor(() => expect(completeUpload).toHaveBeenCalledWith("upload_B", expect.any(String)));
    expect(await screen.findByRole("heading", { name: "second.png" })).toBeInTheDocument();
    await act(async () => {
      resolveFirstCompletion?.(completedUploadResponse(first.upload_id, first.job_id));
      await Promise.resolve();
    });
    expect(screen.getByRole("heading", { name: "second.png" })).toBeInTheDocument();
    expect(screen.queryByText("Artwork verified. Preparation has started.")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Follow preparation" })).not.toBeInTheDocument();
  });

  it("fails closed for an invalid upload identifier without calling the API", () => {
    const getUpload = vi.fn();
    const { api, auth } = dependencies({ getUpload });
    render(<MemoryRouter initialEntries={["/uploads/not.valid"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    expect(screen.getByRole("heading", { name: "This private upload cannot be opened." })).toBeInTheDocument();
    expect(getUpload).not.toHaveBeenCalled();
  });

  it("disables the file and submit controls while the first durable intent is pending", async () => {
    const createUpload = vi.fn().mockImplementation(() => new Promise<never>(() => undefined));
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ createUpload, listJobs });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const user = userEvent.setup();
    const input = screen.getByLabelText(/Choose PNG artwork/u);
    await user.upload(input, makePng());
    const submit = screen.getByRole("button", { name: "Prepare listing" });
    const form = submit.closest("form");
    if (form === null) throw new Error("Upload form is missing");
    fireEvent.submit(form);
    expect(await screen.findByRole("button", { name: "Preparing…" })).toBeDisabled();
    expect(input).toBeDisabled();
    await waitFor(() => expect(createUpload).toHaveBeenCalledTimes(1));
  });
});

function NavigateToUploadB() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => void navigate("/uploads/upload_B")}>Open upload B</button>;
}

function completedUploadResponse(uploadId: string, jobId: string) {
  return {
    value: { upload: { upload_id: uploadId, job_id: jobId, status: "completed" as const, record_version: 2 }, authorization: null },
    requestId: "request-complete",
    etag: null,
  };
}

function dependencies(overrides: Partial<ApiPort>): { api: ApiPort; auth: AuthCoordinator } {
  const never = () => Promise.reject(new Error("Unexpected test call"));
  const api: ApiPort = {
    listJobs: never,
    getJob: never,
    getUpload: never,
    getReview: never,
    createUpload: never,
    authorizeUpload: never,
    completeUpload: never,
    cancelUpload: never,
    reviseListing: never,
    runAction: never,
    fetchArtwork: never,
    ...overrides,
  };
  const session = new MemoryAuthSession();
  session.set("access", 3600, "refresh");
  const auth: AuthCoordinator = { session, startSignIn: never, completeSignIn: never, signOut: vi.fn() };
  return { api, auth };
}

function makePng(): File {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1]);
  const file = new File([bytes], "art.png", { type: "image/png" });
  Object.defineProperty(file, "arrayBuffer", { value: () => Promise.resolve(bytes.buffer.slice(0)) });
  return file;
}
