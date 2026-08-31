import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import browserFixtures from "../../contracts/browser/phase6.5.fixtures.json";
import type { ApiPort } from "../src/api/client";
import { uploadRecoverySchema, type UploadRecovery } from "../src/contracts";
import { MAX_BATCH_FILES, UploadProvider, useUpload } from "../src/upload/upload-context";

const directUpload = vi.hoisted(() => ({
  prepareArtworkForUpload: vi.fn(),
  uploadToAuthorizedS3: vi.fn(),
  validateAndHashPng: vi.fn(),
}));

vi.mock("../src/upload/direct-upload", async (importOriginal) => ({
  ...await importOriginal<Record<string, unknown>>(),
  prepareArtworkForUpload: directUpload.prepareArtworkForUpload,
  uploadToAuthorizedS3: directUpload.uploadToAuthorizedS3,
  validateAndHashPng: directUpload.validateAndHashPng,
}));

const recovery = uploadRecoverySchema.parse(browserFixtures.upload_recovery);

beforeEach(() => {
  directUpload.prepareArtworkForUpload.mockImplementation((file: File) => Promise.resolve({
    file,
    sourceFormat: "png" as const,
  }));
  directUpload.validateAndHashPng.mockImplementation(async (file: File) => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const identityByte = bytes.at(-1) ?? 0;
    return identityByte.toString(16).padStart(2, "0").repeat(32);
  });
  directUpload.uploadToAuthorizedS3.mockImplementation((
    _file: File,
    _authorization: object,
    onProgress: (progress: number) => void,
  ) => {
    onProgress(40);
    onProgress(100);
    return Promise.resolve();
  });
});

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

  it.each([
    ["completed", "complete", "Artwork verified. Preparation has started."],
    ["cancelled", "cancelled", "Upload cancelled."],
    ["expired", "expired", "Upload expired."],
  ] as const)(
    "reconciles a stale local error to durable %s state",
    async (status, phase, message) => {
      const completeUpload = vi.fn().mockRejectedValueOnce(new TypeError("network interrupted"));
      const getUpload = vi.fn().mockResolvedValue(recoveryResult(status));
      const api = fakeApi({ completeUpload, getUpload });
      render(<UploadProvider api={api}><RecoveryHarness /></UploadProvider>);
      const user = userEvent.setup();

      await user.click(screen.getByRole("button", { name: "Recover" }));
      expect(await screen.findByText("network interrupted")).toBeInTheDocument();
      expect(screen.getByTestId("upload-phase")).toHaveTextContent("error");

      await user.click(screen.getByRole("button", { name: "Check durable status" }));
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.getByTestId("upload-phase")).toHaveTextContent(phase);
      expect(getUpload).toHaveBeenCalledWith(recovery.upload_id);
      expect(completeUpload).toHaveBeenCalledTimes(1);
    },
  );

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
    expect(screen.getByText("Choose an artwork file to begin.")).toBeInTheDocument();
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

describe("ordered in-memory upload batches", () => {
  it("creates one upload and job per file in exact order without overlapping work", async () => {
    let resolveFirstCompletion: ((value: ReturnType<typeof completedBatchResult>) => void) | undefined;
    const firstCompletion = new Promise<ReturnType<typeof completedBatchResult>>((resolve) => {
      resolveFirstCompletion = resolve;
    });
    const createUpload = vi.fn((file: File, sha256: string) => (
      Promise.resolve(batchCreateResult(file, sha256))
    ));
    const completeUpload = vi.fn((uploadId: string) => uploadId === "upload_one"
      ? firstCompletion
      : Promise.resolve(completedBatchResult(uploadId)));
    const files = [makePng("one.png", 1), makePng("two.png", 2), makePng("three.png", 3)];
    render(
      <UploadProvider api={fakeApi({ createUpload, completeUpload })}>
        <BatchHarness files={files} />
      </UploadProvider>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Begin batch" }));
    await waitFor(() => expect(completeUpload).toHaveBeenCalledWith(
      "upload_one",
      expect.stringMatching(/^web:complete-upload:/u),
    ));
    expect(createUpload).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("batch-phase")).toHaveTextContent("running");

    await act(async () => {
      resolveFirstCompletion?.(completedBatchResult("upload_one"));
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByTestId("batch-phase")).toHaveTextContent("complete"));

    expect(createUpload.mock.calls.map((call) => call[0].name)).toEqual([
      "one.png",
      "two.png",
      "three.png",
    ]);
    expect(completeUpload.mock.calls.map((call) => call[0])).toEqual([
      "upload_one",
      "upload_two",
      "upload_three",
    ]);
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent("one.pngpngcomplete100upload_onejob_one");
    expect(screen.getByTestId("batch-item-2")).toHaveTextContent("two.pngpngcomplete100upload_twojob_two");
    expect(screen.getByTestId("batch-item-3")).toHaveTextContent("three.pngpngcomplete100upload_threejob_three");
  });

  it("retains a per-file error and continues with the next queued file", async () => {
    const createUpload = vi.fn((file: File, sha256: string) => file.name === "broken.png"
      ? Promise.reject(new Error("simulated per-file failure"))
      : Promise.resolve(batchCreateResult(file, sha256)));
    const completeUpload = vi.fn((uploadId: string) => Promise.resolve(completedBatchResult(uploadId)));
    const files = [makePng("good.png", 1), makePng("broken.png", 2), makePng("later.png", 3)];
    render(
      <UploadProvider api={fakeApi({ createUpload, completeUpload })}>
        <BatchHarness files={files} />
      </UploadProvider>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Begin batch" }));
    await waitFor(() => expect(screen.getByTestId("batch-phase")).toHaveTextContent("complete"));

    expect(createUpload.mock.calls.map((call) => call[0].name)).toEqual([
      "good.png",
      "broken.png",
      "later.png",
    ]);
    expect(completeUpload).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent("complete");
    expect(screen.getByTestId("batch-item-2")).toHaveTextContent("error");
    expect(screen.getByTestId("batch-item-2")).toHaveTextContent("simulated per-file failure");
    expect(screen.getByTestId("batch-item-3")).toHaveTextContent("complete");
    expect(screen.getByTestId("batch-message")).toHaveTextContent(
      "2 of 3 artwork files started preparation; 1 need attention.",
    );
  });

  it("replays a lost create response once with the same batch item identity", async () => {
    const file = makePng("replayed.png", 6);
    const createUpload = vi.fn()
      .mockRejectedValueOnce(new TypeError("simulated lost response"))
      .mockImplementationOnce((createdFile: File, sha256: string) => (
        Promise.resolve(batchCreateResult(createdFile, sha256))
      ));
    const completeUpload = vi.fn((uploadId: string) => Promise.resolve(completedBatchResult(uploadId)));
    render(
      <UploadProvider api={fakeApi({ createUpload, completeUpload })}>
        <BatchHarness files={[file]} />
      </UploadProvider>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Begin batch" }));
    await waitFor(() => expect(screen.getByTestId("batch-phase")).toHaveTextContent("complete"));

    expect(createUpload).toHaveBeenCalledTimes(2);
    expect(createUpload.mock.calls[0]?.[2]).toBe(createUpload.mock.calls[1]?.[2]);
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent("replayed.pngpngcomplete");
  });

  it("preserves the existing recovery route for a post-intent PNG failure", async () => {
    directUpload.uploadToAuthorizedS3.mockRejectedValueOnce(new Error("simulated PNG transfer failure"));
    const createUpload = vi.fn((file: File, sha256: string) => Promise.resolve(batchCreateResult(file, sha256)));
    const file = makePng("recoverable.png", 7);
    render(
      <UploadProvider api={fakeApi({ createUpload })}>
        <BatchHarness files={[file]} />
      </UploadProvider>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Begin batch" }));
    await waitFor(() => expect(screen.getByTestId("batch-phase")).toHaveTextContent("complete"));

    expect(screen.getByTestId("batch-item-1")).toHaveTextContent("recoverable.pngpngerror");
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent("upload_recoverable");
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent("Open recovery to check or resume this exact PNG.");
  });

  it.each([
    ["SVG", "original.svg", "svg", "vector.png"],
    ["JPEG", "original.jpg", "jpeg", "photo.png"],
  ] as const)("cancels a post-intent %s reservation exactly once and continues with the next file", async (
    sourceLabel,
    sourceFilename,
    sourceFormat,
    preparedFilename,
  ) => {
    directUpload.prepareArtworkForUpload.mockImplementation((file: File) => file.name === sourceFilename
      ? Promise.resolve({ file: makePng(preparedFilename, 4), sourceFormat })
      : Promise.resolve({ file, sourceFormat: "png" as const }));
    directUpload.uploadToAuthorizedS3.mockRejectedValueOnce(
      new TypeError(`simulated post-intent ${sourceLabel} failure`),
    );
    const createUpload = vi.fn((file: File, sha256: string) => (
      Promise.resolve(batchCreateResult(file, sha256))
    ));
    const cancelUpload = vi.fn((uploadId: string) => Promise.resolve(cancelledBatchResult(uploadId)));
    const completeUpload = vi.fn((uploadId: string) => Promise.resolve(completedBatchResult(uploadId)));
    const source = sourceFormat === "svg" ? makeSvg(sourceFilename) : makeJpeg(sourceFilename);
    const files = [source, makePng("later.png", 5)];
    render(
      <UploadProvider api={fakeApi({ createUpload, cancelUpload, completeUpload })}>
        <BatchHarness files={files} />
      </UploadProvider>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Begin batch" }));
    await waitFor(() => expect(screen.getByTestId("batch-phase")).toHaveTextContent("complete"));

    expect(createUpload.mock.calls.map((call) => call[0].name)).toEqual([
      preparedFilename,
      "later.png",
    ]);
    expect(cancelUpload).toHaveBeenCalledTimes(1);
    expect(cancelUpload).toHaveBeenCalledWith(
      `upload_${preparedFilename.replace(".png", "")}`,
      expect.stringMatching(/^web:cancel-upload:/u),
    );
    expect(completeUpload).toHaveBeenCalledTimes(1);
    expect(completeUpload).toHaveBeenCalledWith(
      "upload_later",
      expect.stringMatching(/^web:complete-upload:/u),
    );
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent(
      `${sourceFilename}${sourceFormat}error`,
    );
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent(
      `simulated post-intent ${sourceLabel} failure`,
    );
    expect(screen.getByTestId("batch-item-1")).toHaveTextContent(
      `The upload reservation was cancelled. Re-select the original ${sourceLabel} to retry.`,
    );
    expect(screen.getByTestId("batch-item-2")).toHaveTextContent("later.pngpngcomplete");
  });

  it("rejects more than the closed file maximum before calling the API", async () => {
    const createUpload = vi.fn();
    const files = Array.from(
      { length: MAX_BATCH_FILES + 1 },
      (_, index) => makePng(`file-${index + 1}.png`, index),
    );
    render(
      <UploadProvider api={fakeApi({ createUpload })}>
        <BatchHarness files={files} />
      </UploadProvider>,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Begin batch" }));

    expect(screen.getByTestId("batch-phase")).toHaveTextContent("error");
    expect(screen.getByTestId("batch-message")).toHaveTextContent(
      `Choose no more than ${MAX_BATCH_FILES} artwork files at a time.`,
    );
    expect(screen.queryAllByTestId(/^batch-item-/u)).toHaveLength(0);
    expect(createUpload).not.toHaveBeenCalled();
  });
});

function RecoveryHarness() {
  const upload = useUpload();
  return (
    <div>
      <p>{upload.state.message}</p>
      <p data-testid="upload-id">{upload.state.uploadId ?? "none"}</p>
      <p data-testid="upload-phase">{upload.state.phase}</p>
      <button type="button" onClick={() => { void upload.recover(recovery); }}>Recover</button>
      <button type="button" onClick={() => { void upload.recover(); }}>Check durable status</button>
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

function BatchHarness({ files }: { files: readonly File[] }) {
  const upload = useUpload();
  return (
    <div>
      <p data-testid="batch-phase">{upload.batch.phase}</p>
      <p data-testid="batch-message">{upload.batch.message}</p>
      <button type="button" onClick={() => { void upload.beginBatch(files); }}>Begin batch</button>
      <ol>
        {upload.batch.items.map((item) => (
          <li data-testid={`batch-item-${item.position}`} key={item.id}>
            {item.filename}
            {item.sourceFormat ?? "pending"}
            {item.phase}
            {item.progress}
            {item.uploadId ?? "none"}
            {item.jobId ?? "none"}
            {item.message}
            {item.error ?? ""}
          </li>
        ))}
      </ol>
    </div>
  );
}

function makePng(name: string, tail: number): File {
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, tail]);
  const file = new File([bytes], name, { type: "image/png", lastModified: 123 });
  Object.defineProperty(file, "arrayBuffer", { value: () => Promise.resolve(bytes.buffer.slice(0)) });
  return file;
}

function makeSvg(name: string): File {
  return new File([
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>',
  ], name, { type: "image/svg+xml", lastModified: 123 });
}

function makeJpeg(name: string): File {
  return new File([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], name, {
    type: "image/jpeg",
    lastModified: 123,
  });
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

function recoveryResult(status: Exclude<UploadRecovery["status"], "open">) {
  return {
    value: {
      ...recovery,
      status,
      completed_at: status === "completed" ? recovery.updated_at : null,
      cancelled_at: status === "cancelled" ? recovery.updated_at : null,
      expired_at: status === "expired" ? recovery.updated_at : null,
    },
    requestId: `request-recovery-${status}`,
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

function batchCreateResult(file: File, sha256: string) {
  const stem = file.name.replace(/\.[^.]+$/u, "").replace(/[^A-Za-z0-9_-]/gu, "-");
  const uploadId = `upload_${stem}`;
  const jobId = `job_${stem}`;
  return {
    value: {
      upload: { upload_id: uploadId, job_id: jobId, status: "open" as const, record_version: 0 },
      authorization: {
        upload_id: uploadId,
        job_id: jobId,
        authorization_generation: 1,
        method: "POST" as const,
        url: "https://private-bucket.s3.us-west-2.amazonaws.com/",
        form_fields: { key: `private/${stem}.png` },
        content_sha256: sha256,
        size_bytes: file.size,
        issued_at: "2026-08-30T12:00:00Z",
        expires_at: "2026-08-30T12:05:00Z",
      },
    },
    requestId: `request-create-${stem}`,
    etag: null,
  };
}

function completedBatchResult(uploadId: string) {
  const stem = uploadId.slice("upload_".length);
  return {
    value: {
      upload: {
        upload_id: uploadId,
        job_id: `job_${stem}`,
        status: "completed" as const,
        record_version: 1,
      },
      authorization: null,
    },
    requestId: `request-complete-${stem}`,
    etag: null,
  };
}

function cancelledBatchResult(uploadId: string) {
  const stem = uploadId.slice("upload_".length);
  return {
    value: {
      upload: {
        upload_id: uploadId,
        job_id: `job_${stem}`,
        status: "cancelled" as const,
        record_version: 1,
      },
      authorization: null,
    },
    requestId: `request-cancel-${stem}`,
    etag: null,
  };
}
