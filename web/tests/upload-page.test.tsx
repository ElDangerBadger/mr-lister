import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import type { ApiPort } from "../src/api/client";
import { AppRoutes } from "../src/App";
import { MemoryAuthSession, type AuthCoordinator } from "../src/auth/session";
import { uploadRecoverySchema } from "../src/contracts";

const directUpload = vi.hoisted(() => ({
  uploadToAuthorizedS3: vi.fn(),
  validateAndHashPng: vi.fn(),
}));

vi.mock("../src/upload/direct-upload", async (importOriginal) => ({
  ...await importOriginal<Record<string, unknown>>(),
  uploadToAuthorizedS3: directUpload.uploadToAuthorizedS3,
  validateAndHashPng: directUpload.validateAndHashPng,
}));

beforeEach(() => {
  directUpload.uploadToAuthorizedS3.mockResolvedValue(undefined);
  directUpload.validateAndHashPng.mockResolvedValue("a".repeat(64));
});

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
    const input = screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u);
    await user.upload(input, makePng());
    const submit = screen.getByRole("button", { name: "Prepare 1 listing" });
    const form = submit.closest("form");
    if (form === null) throw new Error("Upload form is missing");
    fireEvent.submit(form);
    expect(await screen.findByRole("button", { name: "Preparing batch…" })).toBeDisabled();
    expect(input).toBeDisabled();
    await waitFor(() => expect(createUpload).toHaveBeenCalledTimes(1));
  });

  it("clears and restores the recent list without deleting authoritative work", async () => {
    const user = userEvent.setup();
    const jobs = [
      recentJob("job_recent_one", "2026-09-04T12:00:00Z"),
      recentJob("job_recent_two", "2026-09-03T12:00:00Z"),
    ];
    const listJobs = vi.fn().mockResolvedValue({
      value: { jobs, next_cursor: null },
      requestId: "request-jobs",
      etag: null,
    });
    const { api, auth } = dependencies({ listJobs });
    const first = render(
      <MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>,
    );

    expect(await screen.findAllByRole("link", { name: /Open preparation/u })).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Clear list from this browser" }));

    expect(screen.getByText("Recent list cleared.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Open preparation/u })).not.toBeInTheDocument();
    expect(screen.getByText(/publication records, and audit history are preserved/u)).toBeVisible();

    first.unmount();
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    expect(await screen.findByText("Recent list cleared.")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Show recent list" }));
    expect(await screen.findAllByRole("link", { name: /Open preparation/u })).toHaveLength(2);
    expect(listJobs).toHaveBeenCalledTimes(2);
  });

  it("keeps a selected batch in an explicit seller-controlled order", async () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ listJobs });
    const result = render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const user = userEvent.setup();
    const first = makePng("first.png", 1);
    const second = makePng("second.png", 2);
    const input = screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u);

    await user.upload(input, [first, second]);
    expect(selectedNames(result.container)).toEqual(["first.png", "second.png"]);
    await user.click(screen.getByRole("button", { name: "Move second.png earlier" }));
    expect(selectedNames(result.container)).toEqual(["second.png", "first.png"]);
    expect(screen.getByRole("button", { name: "Prepare 2 listings" })).toBeEnabled();
  });

  it("accepts an ordered file-only drop while preserving the native picker fallback", () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ listJobs });
    const result = render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const first = makePng("first.png", 1);
    const second = makePng("second.png", 2);
    const dropField = screen.getByText(/Drag and drop PNG, SVG, or JPEG artwork/u).closest("label");
    if (dropField === null) throw new Error("Drop field is missing");

    fireEvent.dragEnter(dropField, { dataTransfer: { types: ["Files"], files: [first, second], dropEffect: "none" } });
    expect(dropField).toHaveAttribute("data-drag-active", "true");
    fireEvent.drop(dropField, { dataTransfer: { types: ["Files"], files: [first, second], dropEffect: "copy" } });

    expect(dropField).toHaveAttribute("data-drag-active", "false");
    expect(selectedNames(result.container)).toEqual(["first.png", "second.png"]);
    expect(screen.getByRole("button", { name: "Prepare 2 listings" })).toBeEnabled();
    expect(screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u)).toHaveAttribute("type", "file");
  });

  it("keeps a nested file drag highlighted until it leaves the whole drop field", () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ listJobs });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const prompt = screen.getByText(/Drag and drop PNG, SVG, or JPEG artwork/u);
    const dropField = prompt.closest("label");
    if (dropField === null) throw new Error("Drop field is missing");
    const transfer = { types: ["Files"], files: [], dropEffect: "none" };

    fireEvent.dragEnter(dropField, { dataTransfer: transfer });
    fireEvent.dragEnter(prompt, { dataTransfer: transfer });
    fireEvent.dragLeave(prompt, { dataTransfer: transfer });
    expect(dropField).toHaveAttribute("data-drag-active", "true");
    fireEvent.dragLeave(dropField, { dataTransfer: transfer });
    expect(dropField).toHaveAttribute("data-drag-active", "false");
  });

  it("rejects a dropped selection above the five-file cap and ignores non-file drops", () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ listJobs });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const dropField = screen.getByText(/Drag and drop PNG, SVG, or JPEG artwork/u).closest("label");
    if (dropField === null) throw new Error("Drop field is missing");

    fireEvent.drop(dropField, { dataTransfer: { types: ["text/plain"], files: [], dropEffect: "none" } });
    expect(screen.getByRole("button", { name: "Choose artwork to continue" })).toBeDisabled();
    fireEvent.drop(dropField, {
      dataTransfer: {
        types: ["Files"],
        files: Array.from({ length: 6 }, (_, index) => makePng(`drop-${index + 1}.png`, index)),
        dropEffect: "copy",
      },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Choose no more than 5 files");
    expect(screen.getByRole("button", { name: "Choose artwork to continue" })).toBeDisabled();
  });

  it("keeps unsupported dropped files isolated for per-file batch feedback", async () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const createUpload = vi.fn((file: File, sha256: string) => Promise.resolve(openUploadResponse(file, sha256)));
    const completeUpload = vi.fn().mockResolvedValue(completedUploadResponse("upload_art", "job_art"));
    const { api, auth } = dependencies({ listJobs, createUpload, completeUpload });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const dropField = screen.getByText(/Drag and drop PNG, SVG, or JPEG artwork/u).closest("label");
    if (dropField === null) throw new Error("Drop field is missing");

    fireEvent.drop(dropField, {
      dataTransfer: {
        types: ["Files"],
        files: [
          new File(["not artwork"], "notes.txt", { type: "text/plain" }),
          makePng("valid.png", 2),
        ],
        dropEffect: "copy",
      },
    });

    expect(screen.getByText(/Unsupported file · this item will be rejected/u)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prepare 2 listings" })).toBeEnabled();
    submitBatchForm(2);
    expect(await screen.findByText(
      "1 of 2 artwork files started preparation; 1 need attention.",
    )).toBeInTheDocument();
    expect(screen.getByText("Choose PNG, compatible SVG, or JPEG artwork files.")).toBeInTheDocument();
    expect(createUpload).toHaveBeenCalledTimes(1);
    const reset = screen.getByRole("button", { name: "Choose another batch" });
    expect(reset).toBeEnabled();
    await userEvent.setup().click(reset);
    expect(screen.getByRole("button", { name: "Choose artwork to continue" })).toBeDisabled();
    expect(screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u)).toBeEnabled();
  });

  it("ignores file drops while an upload batch is locked", async () => {
    const createUpload = vi.fn().mockImplementation(() => new Promise<never>(() => undefined));
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ createUpload, listJobs });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const user = userEvent.setup();
    await user.upload(screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u), makePng("first.png", 1));
    submitBatchForm();
    expect(await screen.findByRole("button", { name: "Preparing batch…" })).toBeDisabled();
    const dropField = screen.getByText(/Drag and drop PNG, SVG, or JPEG artwork/u).closest("label");
    if (dropField === null) throw new Error("Drop field is missing");

    fireEvent.drop(dropField, {
      dataTransfer: { types: ["Files"], files: [makePng("ignored.png", 2)], dropEffect: "none" },
    });

    expect(dropField).toHaveAttribute("data-drag-active", "false");
    await waitFor(() => expect(createUpload).toHaveBeenCalledTimes(1));
  });

  it("rejects a selection above the five-file MVP cap", async () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const { api, auth } = dependencies({ listJobs });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const user = userEvent.setup();

    await user.upload(
      screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u),
      Array.from({ length: 6 }, (_, index) => makePng(`art-${index + 1}.png`, index)),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Choose no more than 5 files");
    expect(screen.getByRole("button", { name: "Choose artwork to continue" })).toBeDisabled();
  });

  it("locks a completed batch until the seller explicitly starts another", async () => {
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const createUpload = vi.fn((file: File, sha256: string) => Promise.resolve(openUploadResponse(file, sha256)));
    const completeUpload = vi.fn().mockResolvedValue(completedUploadResponse("upload_art", "job_art"));
    const { api, auth } = dependencies({ listJobs, createUpload, completeUpload });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const user = userEvent.setup();
    const input = screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u);

    await user.upload(input, makePng());
    submitBatchForm();
    expect(await screen.findByRole("button", { name: "Batch complete" })).toBeDisabled();
    expect(input).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Choose another batch" }));
    expect(input).toBeEnabled();
    expect(screen.getByRole("button", { name: "Choose artwork to continue" })).toBeDisabled();
  });

  it("links a failed PNG item to the existing upload recovery route", async () => {
    directUpload.uploadToAuthorizedS3.mockRejectedValueOnce(new Error("simulated transfer interruption"));
    const listJobs = vi.fn().mockResolvedValue({ value: { jobs: [], next_cursor: null }, requestId: "request-jobs", etag: null });
    const createUpload = vi.fn((file: File, sha256: string) => Promise.resolve(openUploadResponse(file, sha256)));
    const { api, auth } = dependencies({ listJobs, createUpload });
    render(<MemoryRouter initialEntries={["/"]}><AppRoutes dependencies={{ api, auth }} /></MemoryRouter>);
    const user = userEvent.setup();

    await user.upload(screen.getByLabelText(/Drag and drop PNG, SVG, or JPEG artwork/u), makePng());
    submitBatchForm();

    const recoveryLink = await screen.findByRole("link", { name: "Recover upload" });
    expect(recoveryLink).toHaveAttribute("href", "/uploads/upload_art");
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

function openUploadResponse(file: File, sha256: string) {
  return {
    value: {
      upload: { upload_id: "upload_art", job_id: "job_art", status: "open" as const, record_version: 1 },
      authorization: {
        upload_id: "upload_art",
        job_id: "job_art",
        authorization_generation: 1,
        method: "POST" as const,
        url: "https://private-bucket.s3.us-west-2.amazonaws.com/",
        form_fields: { key: "private/art.png" },
        content_sha256: sha256,
        size_bytes: file.size,
        issued_at: "2026-08-30T12:00:00Z",
        expires_at: "2026-08-30T12:05:00Z",
      },
    },
    requestId: "request-create",
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

function makePng(name = "art.png", tail = 1): File {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, tail]);
  const file = new File([bytes], name, { type: "image/png" });
  Object.defineProperty(file, "arrayBuffer", { value: () => Promise.resolve(bytes.buffer.slice(0)) });
  return file;
}

function recentJob(jobId: string, updatedAt: string) {
  return {
    job_id: jobId,
    state: "approved" as const,
    record_version: 8,
    review_version: 3,
    created_at: updatedAt,
    updated_at: updatedAt,
  };
}

function selectedNames(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".selection-list strong")].map((element) => element.textContent ?? "");
}

function submitBatchForm(fileCount = 1): void {
  const submit = screen.getByRole("button", {
    name: fileCount === 1 ? "Prepare 1 listing" : `Prepare ${fileCount} listings`,
  });
  const form = submit.closest("form");
  if (form === null) throw new Error("Upload form is missing");
  fireEvent.submit(form);
}
