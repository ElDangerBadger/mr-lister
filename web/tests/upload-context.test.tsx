import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import type { ApiPort } from "../src/api/client";
import { uploadRecoverySchema } from "../src/contracts";
import { UploadProvider, useUpload } from "../src/upload/upload-context";

const recovery = uploadRecoverySchema.parse(browserFixtures.upload_recovery);

describe("durable upload operation identity", () => {
  it("reuses the exact completion key after a lost completion response", async () => {
    const completeUpload = vi.fn()
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce(completedResponse());
    const api = fakeApi({ completeUpload });
    render(<UploadProvider api={api}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Recover" }));
    expect(await screen.findByText("network interrupted")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Recover" }));
    expect(await screen.findByText("Artwork verified. Preparation has started.")).toBeInTheDocument();
    expect(completeUpload).toHaveBeenCalledTimes(2);
    expect(completeUpload.mock.calls[0]?.[1]).toBe(completeUpload.mock.calls[1]?.[1]);
  });

  it("reuses the exact cancellation key when the first response is lost", async () => {
    const cancelUpload = vi.fn()
      .mockRejectedValueOnce(new TypeError("network interrupted"))
      .mockResolvedValueOnce(cancelledResponse());
    const api = fakeApi({ cancelUpload });
    render(<UploadProvider api={api}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Recover complete" }));
    await screen.findByText("Artwork verified. Preparation has started.");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("network interrupted")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Upload cancelled.")).toBeInTheDocument();
    expect(cancelUpload).toHaveBeenCalledTimes(2);
    expect(cancelUpload.mock.calls[0]?.[1]).toBe(cancelUpload.mock.calls[1]?.[1]);
  });

  it("clears the completed upload identity before starting a second file", async () => {
    const createUpload = vi.fn().mockImplementation(() => new Promise<never>(() => undefined));
    const api = fakeApi({ createUpload });
    render(<UploadProvider api={api}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Recover complete" }));
    await screen.findByText("Artwork verified. Preparation has started.");
    expect(screen.getByTestId("upload-id")).toHaveTextContent(recovery.upload_id);
    await user.click(screen.getByRole("button", { name: "Begin second" }));
    expect(screen.getByTestId("upload-id")).toHaveTextContent("none");
  });

  it("retains replayed upload IDs and renews when create authorization has expired", async () => {
    const createUpload = vi.fn().mockResolvedValue({
      value: { upload: { upload_id: "upload_replayed", job_id: "job_replayed", status: "open", record_version: 0 }, authorization: null },
      requestId: "request-create",
      etag: null,
    });
    const authorizeUpload = vi.fn().mockRejectedValue(new TypeError("reauthorization interrupted"));
    render(<UploadProvider api={fakeApi({ createUpload, authorizeUpload })}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Begin second" }));
    expect(await screen.findByText("reauthorization interrupted")).toBeInTheDocument();
    expect(screen.getByTestId("upload-id")).toHaveTextContent("upload_replayed");
    expect(authorizeUpload).toHaveBeenCalledWith("upload_replayed", expect.stringMatching(/^web:reauthorize-upload:/u));
  });

  it("binds a retained create key to the exact content fingerprint", async () => {
    const createUpload = vi.fn().mockRejectedValue(new TypeError("create response lost"));
    render(<UploadProvider api={fakeApi({ createUpload })}><FingerprintHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "First bytes" }));
    await screen.findByText("create response lost");
    await user.click(screen.getByRole("button", { name: "Second bytes" }));
    await screen.findByText("create response lost");
    expect(createUpload).toHaveBeenCalledTimes(2);
    expect(createUpload.mock.calls[0]?.[2]).not.toBe(createUpload.mock.calls[1]?.[2]);
  });

  it("serializes duplicate begins until the first durable intent is discoverable", async () => {
    const createUpload = vi.fn().mockImplementation(() => new Promise<never>(() => undefined));
    render(<UploadProvider api={fakeApi({ createUpload })}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    const begin = screen.getByRole("button", { name: "Begin second" });
    await user.click(begin);
    await user.click(begin);
    await waitFor(() => expect(createUpload).toHaveBeenCalledTimes(1));
  });

  it("rotates an accepted authorization key only after durable expiry reconciliation", async () => {
    const uploadId = "upload_replayed";
    const createUpload = vi.fn().mockResolvedValue({
      value: { upload: { upload_id: uploadId, job_id: "job_replayed", status: "open", record_version: 0 }, authorization: null },
      requestId: "request-create",
      etag: null,
    });
    const authorizeUpload = vi.fn()
      .mockResolvedValueOnce({
        value: { upload: { upload_id: uploadId, job_id: "job_replayed", status: "open", record_version: 1 }, authorization: null },
        requestId: "request-replayed-authorize",
        etag: null,
      })
      .mockRejectedValueOnce(new TypeError("fresh authorization interrupted"));
    const getUpload = vi.fn().mockResolvedValue({
      value: {
        ...recovery,
        upload_id: uploadId,
        job_id: "job_replayed",
        filename: "second.png",
        size_bytes: 9,
        authorization_expires_at: new Date(Date.now() - 60_000).toISOString(),
      },
      requestId: "request-recovery",
      etag: null,
    });
    render(<UploadProvider api={fakeApi({ createUpload, authorizeUpload, getUpload })}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Begin second" }));
    expect(await screen.findByText("fresh authorization interrupted")).toBeInTheDocument();
    expect(authorizeUpload).toHaveBeenCalledTimes(2);
    expect(authorizeUpload.mock.calls[0]?.[1]).not.toBe(authorizeUpload.mock.calls[1]?.[1]);
  });

  it("ignores a delayed completion after a local reset", async () => {
    let resolveCompletion: ((value: ReturnType<typeof completedResult>) => void) | undefined;
    const completion = new Promise<ReturnType<typeof completedResult>>((resolve) => { resolveCompletion = resolve; });
    const completeUpload = vi.fn().mockReturnValue(completion);
    render(<UploadProvider api={fakeApi({ completeUpload })}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Recover" }));
    await waitFor(() => expect(completeUpload).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByTestId("upload-id")).toHaveTextContent("none");
    await act(async () => {
      resolveCompletion?.(completedResult());
      await Promise.resolve();
    });
    expect(screen.getByTestId("upload-id")).toHaveTextContent("none");
    expect(screen.getByText("Choose one PNG artwork file to begin.")).toBeInTheDocument();
  });

  it("ignores a delayed completion after cancellation takes authority", async () => {
    let resolveCompletion: ((value: ReturnType<typeof completedResult>) => void) | undefined;
    const completion = new Promise<ReturnType<typeof completedResult>>((resolve) => { resolveCompletion = resolve; });
    const completeUpload = vi.fn().mockReturnValue(completion);
    const cancelUpload = vi.fn().mockResolvedValue(await cancelledResponse());
    render(<UploadProvider api={fakeApi({ completeUpload, cancelUpload })}><RecoveryHarness /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Recover" }));
    await waitFor(() => expect(completeUpload).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Upload cancelled.")).toBeInTheDocument();
    await act(async () => {
      resolveCompletion?.(completedResult());
      await Promise.resolve();
    });
    expect(screen.getByText("Upload cancelled.")).toBeInTheDocument();
    expect(screen.queryByText("Artwork verified. Preparation has started.")).not.toBeInTheDocument();
  });
});

function RecoveryHarness() {
  const upload = useUpload();
  return (
    <div>
      <p>{upload.state.message}</p>
      <p data-testid="upload-id">{upload.state.uploadId ?? "none"}</p>
      <button type="button" onClick={() => { void upload.recover(recovery); }}>Recover</button>
      <button type="button" onClick={() => { void upload.recover({ ...recovery, status: "completed", completed_at: recovery.updated_at }); }}>Recover complete</button>
      <button type="button" onClick={() => { void upload.cancel(); }}>Cancel</button>
      <button type="button" onClick={() => upload.reset()}>Reset</button>
      <button type="button" onClick={() => {
        void upload.begin(makePng("second.png", 0));
      }}>Begin second</button>
    </div>
  );
}

function FingerprintHarness() {
  const upload = useUpload();
  const begin = (tail: number) => {
    void upload.begin(makePng("same.png", tail));
  };
  return <div><p>{upload.state.message}</p><button type="button" onClick={() => begin(1)}>First bytes</button><button type="button" onClick={() => begin(2)}>Second bytes</button></div>;
}

function makePng(name: string, tail: number): File {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, tail]);
  const file = new File([bytes], name, { type: "image/png", lastModified: 123 });
  Object.defineProperty(file, "arrayBuffer", { value: () => Promise.resolve(bytes.buffer.slice(0)) });
  return file;
}

function fakeApi(overrides: Partial<ApiPort>): ApiPort {
  const never = () => Promise.reject(new Error("Unexpected test call"));
  return {
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
}

function completedResponse() {
  return Promise.resolve(completedResult());
}

function completedResult() {
  return {
    value: { upload: { upload_id: recovery.upload_id, job_id: recovery.job_id, status: "completed" as const, record_version: 1 }, authorization: null },
    requestId: "request-complete",
    etag: null,
  };
}

function cancelledResponse() {
  return Promise.resolve({
    value: { upload: { upload_id: recovery.upload_id, job_id: recovery.job_id, status: "cancelled" as const, record_version: 1 }, authorization: null },
    requestId: "request-cancel",
    etag: null,
  });
}
