import { createContext, useCallback, useContext, useMemo, useReducer, useRef, type ReactNode } from "react";
import { ApiError, newIdempotencyKey, type ApiPort } from "../api/client";
import type { UploadRecovery } from "../contracts";
import {
  type ArtworkSourceFormat,
  prepareArtworkForUpload,
  uploadToAuthorizedS3,
  validateAndHashPng,
} from "./direct-upload";

export const MAX_BATCH_FILES = 5;

export type UploadPhase =
  | "idle"
  | "validating"
  | "hashing"
  | "creating_intent"
  | "uploading"
  | "finalizing"
  | "complete"
  | "cancelling"
  | "cancelled"
  | "expired"
  | "error";

export interface UploadState {
  phase: UploadPhase;
  progress: number;
  uploadId: string | null;
  jobId: string | null;
  filename: string | null;
  message: string;
  requestId: string | null;
}

export type BatchUploadItemPhase =
  | "queued"
  | "validating"
  | "hashing"
  | "creating_intent"
  | "uploading"
  | "finalizing"
  | "complete"
  | "expired"
  | "error";

export interface BatchUploadItemState {
  id: string;
  position: number;
  filename: string;
  preparedFilename: string | null;
  sizeBytes: number;
  sourceFormat: ArtworkSourceFormat | null;
  phase: BatchUploadItemPhase;
  progress: number;
  uploadId: string | null;
  jobId: string | null;
  message: string;
  error: string | null;
  requestId: string | null;
}

export interface UploadBatchState {
  phase: "idle" | "running" | "complete" | "error";
  items: readonly BatchUploadItemState[];
  message: string;
}

type UploadAction =
  | { type: "begin" }
  | { type: "phase"; phase: UploadPhase; message: string }
  | { type: "terminal"; phase: "complete" | "cancelled" | "expired"; message: string }
  | { type: "intent"; uploadId: string; jobId: string; filename: string }
  | { type: "progress"; progress: number }
  | { type: "failure"; message: string; requestId: string | null; expired: boolean }
  | { type: "reset" };

const initialState: UploadState = {
  phase: "idle",
  progress: 0,
  uploadId: null,
  jobId: null,
  filename: null,
  message: "Choose an artwork file to begin.",
  requestId: null,
};

const initialBatchState: UploadBatchState = {
  phase: "idle",
  items: [],
  message: `Choose up to ${MAX_BATCH_FILES} artwork files to begin.`,
};

type UploadBatchAction =
  | { type: "start"; items: BatchUploadItemState[] }
  | { type: "item"; id: string; changes: Partial<BatchUploadItemState> }
  | { type: "finish"; failed: number }
  | { type: "failure"; message: string }
  | { type: "reset" };

interface UploadContextValue {
  state: UploadState;
  batch: UploadBatchState;
  begin(file: File): Promise<void>;
  beginBatch(files: Iterable<File>): Promise<void>;
  resume(file: File, recovery: UploadRecovery): Promise<void>;
  recover(recovery?: UploadRecovery): Promise<void>;
  cancel(): Promise<void>;
  reset(): void;
}

const UploadContext = createContext<UploadContextValue | null>(null);

export function UploadProvider({ api, children }: { api: ApiPort; children: ReactNode }) {
  const [state, dispatch] = useReducer(uploadReducer, initialState);
  const [batch, dispatchBatch] = useReducer(uploadBatchReducer, initialBatchState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const abortRef = useRef<AbortController | null>(null);
  const batchAbortRef = useRef<AbortController | null>(null);
  const batchActiveRef = useRef(false);
  const batchEpoch = useRef(0);
  const batchFiles = useRef(new Map<string, File>());
  const recoveryKeys = useRef(new Map<string, UploadOperationKeys>());
  const cancellationKeys = useRef(new Map<string, string>());
  const createKey = useRef<{ fileIdentity: string; value: string } | null>(null);
  const preIntentBeginActive = useRef<symbol | null>(null);
  const operationEpoch = useRef(0);

  const reconcileTerminalUpload = useCallback((
    uploadId: string,
    status: UploadRecovery["status"],
  ): boolean => {
    if (status === "open") return false;
    recoveryKeys.current.delete(uploadId);
    cancellationKeys.current.delete(uploadId);
    if (status === "completed") {
      dispatch({ type: "terminal", phase: "complete", message: "Artwork verified. Preparation has started." });
    } else if (status === "cancelled") {
      dispatch({ type: "terminal", phase: "cancelled", message: "Upload cancelled." });
    } else {
      dispatch({ type: "terminal", phase: "expired", message: "Upload expired." });
    }
    return true;
  }, []);

  const begin = useCallback(async (file: File): Promise<void> => {
    if (batchActiveRef.current || preIntentBeginActive.current !== null) return;
    batchFiles.current.clear();
    dispatchBatch({ type: "reset" });
    const beginOperation = Symbol("pre-intent-upload");
    preIntentBeginActive.current = beginOperation;
    const epoch = ++operationEpoch.current;
    abortRef.current?.abort();
    const abort = new AbortController();
    abortRef.current = abort;
    try {
      dispatch({ type: "begin" });
      await Promise.resolve();
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      dispatch({ type: "phase", phase: "hashing", message: "Calculating the artwork fingerprint…" });
      const sha256 = await validateAndHashPng(file);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      dispatch({ type: "phase", phase: "creating_intent", message: "Reserving a private upload…" });
      const fileIdentity = `${file.name}:${file.size}:${sha256}`;
      if (createKey.current?.fileIdentity !== fileIdentity) {
        createKey.current = { fileIdentity, value: newIdempotencyKey("create-upload") };
      }
      const created = await api.createUpload(file, sha256, createKey.current.value);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      dispatch({
        type: "intent",
        uploadId: created.value.upload.upload_id,
        jobId: created.value.upload.job_id,
        filename: file.name,
      });
      if (preIntentBeginActive.current === beginOperation) preIntentBeginActive.current = null;
      createKey.current = null;
      const keys = recoveryKeys.current.get(created.value.upload.upload_id) ?? {
        authorize: newIdempotencyKey("reauthorize-upload"),
        complete: newIdempotencyKey("complete-upload"),
      };
      recoveryKeys.current.set(created.value.upload.upload_id, keys);
      let authorization = created.value.authorization;
      if (authorization === null) {
        dispatch({ type: "phase", phase: "creating_intent", message: "Renewing the expired private upload authorization…" });
        authorization = await obtainFreshAuthorization(
          api,
          created.value.upload.upload_id,
          created.value.upload.job_id,
          keys,
          () => operationEpoch.current === epoch && !abort.signal.aborted,
        );
      }
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (authorization === null || authorization.content_sha256 !== sha256) {
        throw new Error("The upload service did not return a matching authorization.");
      }
      dispatch({ type: "phase", phase: "uploading", message: "Uploading artwork…" });
      await uploadToAuthorizedS3(file, authorization, (progress) => {
        if (operationEpoch.current === epoch) dispatch({ type: "progress", progress });
      }, abort.signal);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      dispatch({ type: "phase", phase: "finalizing", message: "Verifying the exact uploaded file…" });
      const completed = await api.completeUpload(created.value.upload.upload_id, keys.complete);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (completed.value.upload.upload_id !== created.value.upload.upload_id
        || completed.value.upload.job_id !== created.value.upload.job_id
        || completed.value.upload.status !== "completed") {
        throw new Error("The upload did not reach its completed state.");
      }
      recoveryKeys.current.delete(created.value.upload.upload_id);
      dispatch({ type: "phase", phase: "complete", message: "Artwork verified. Preparation has started." });
    } catch (error) {
      if (operationEpoch.current !== epoch || (error instanceof DOMException && error.name === "AbortError")) return;
      const apiError = error instanceof ApiError ? error : null;
      dispatch({
        type: "failure",
        message: error instanceof Error ? error.message : "The upload could not be completed.",
        requestId: apiError?.requestId ?? null,
        expired: apiError?.status === 410,
      });
    } finally {
      if (preIntentBeginActive.current === beginOperation) preIntentBeginActive.current = null;
    }
  }, [api]);

  const beginBatch = useCallback(async (selection: Iterable<File>): Promise<void> => {
    if (batchActiveRef.current) return;
    const files = Array.from(selection);
    if (files.length === 0) {
      batchFiles.current.clear();
      dispatchBatch({ type: "reset" });
      return;
    }
    if (files.length > MAX_BATCH_FILES) {
      batchFiles.current.clear();
      dispatchBatch({
        type: "failure",
        message: `Choose no more than ${MAX_BATCH_FILES} artwork files at a time.`,
      });
      return;
    }

    operationEpoch.current += 1;
    abortRef.current?.abort();
    preIntentBeginActive.current = null;
    createKey.current = null;
    dispatch({ type: "reset" });

    const epoch = ++batchEpoch.current;
    const abort = new AbortController();
    batchAbortRef.current?.abort();
    batchAbortRef.current = abort;
    batchActiveRef.current = true;
    const items = files.map<BatchUploadItemState>((file, index) => ({
      id: crypto.randomUUID(),
      position: index + 1,
      filename: file.name,
      preparedFilename: null,
      sizeBytes: file.size,
      sourceFormat: null,
      phase: "queued",
      progress: 0,
      uploadId: null,
      jobId: null,
      message: "Queued for private upload.",
      error: null,
      requestId: null,
    }));
    const createKeys = new Map(items.map((item) => [item.id, newIdempotencyKey("create-upload")]));
    batchFiles.current = new Map(items.map((item, index) => [item.id, files[index]!]));
    dispatchBatch({ type: "start", items });

    let failed = 0;
    try {
      for (const item of items) {
        if (abort.signal.aborted || batchEpoch.current !== epoch) return;
        const sourceFile = batchFiles.current.get(item.id);
        if (sourceFile === undefined) return;
        let sourceFormat: ArtworkSourceFormat | null = null;
        let createdUploadId: string | null = null;
        let createdJobId: string | null = null;
        try {
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: {
              phase: "validating",
              message: "Checking artwork…",
              error: null,
              requestId: null,
            },
          });
          const prepared = await prepareArtworkForUpload(sourceFile);
          sourceFormat = prepared.sourceFormat;
          if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: {
              preparedFilename: prepared.file.name,
              sourceFormat: prepared.sourceFormat,
              phase: "hashing",
              message: "Calculating the artwork fingerprint…",
            },
          });
          const sha256 = await validateAndHashPng(prepared.file);
          if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: { phase: "creating_intent", message: "Reserving a private upload…" },
          });
          const createIdempotencyKey = createKeys.get(item.id);
          if (createIdempotencyKey === undefined) throw new Error("The batch upload identity is unavailable.");
          const created = await createUploadWithNetworkReplay(
            api,
            prepared.file,
            sha256,
            createIdempotencyKey,
            () => batchEpoch.current === epoch && !abort.signal.aborted,
          );
          if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          const uploadId = created.value.upload.upload_id;
          const jobId = created.value.upload.job_id;
          createdUploadId = uploadId;
          createdJobId = jobId;
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: { uploadId, jobId },
          });
          const keys = recoveryKeys.current.get(uploadId) ?? {
            authorize: newIdempotencyKey("reauthorize-upload"),
            complete: newIdempotencyKey("complete-upload"),
          };
          recoveryKeys.current.set(uploadId, keys);
          let authorization = created.value.authorization;
          if (authorization === null) {
            dispatchBatch({
              type: "item",
              id: item.id,
              changes: {
                phase: "creating_intent",
                message: "Renewing the expired private upload authorization…",
              },
            });
            authorization = await obtainFreshAuthorization(
              api,
              uploadId,
              jobId,
              keys,
              () => batchEpoch.current === epoch && !abort.signal.aborted,
            );
          }
          if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          if (authorization === null || authorization.content_sha256 !== sha256) {
            throw new Error("The upload service did not return a matching authorization.");
          }
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: { phase: "uploading", message: "Uploading artwork…" },
          });
          await uploadToAuthorizedS3(prepared.file, authorization, (progress) => {
            if (batchEpoch.current === epoch && !abort.signal.aborted) {
              dispatchBatch({ type: "item", id: item.id, changes: { progress } });
            }
          }, abort.signal);
          if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: { phase: "finalizing", message: "Verifying the exact uploaded file…" },
          });
          const completed = await api.completeUpload(uploadId, keys.complete);
          if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          if (completed.value.upload.upload_id !== uploadId
            || completed.value.upload.job_id !== jobId
            || completed.value.upload.status !== "completed") {
            throw new Error("The upload did not reach its completed state.");
          }
          recoveryKeys.current.delete(uploadId);
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: {
              phase: "complete",
              progress: 100,
              message: "Artwork verified. Preparation has started.",
            },
          });
        } catch (error) {
          if (abort.signal.aborted || batchEpoch.current !== epoch
            || (error instanceof DOMException && error.name === "AbortError")) return;
          failed += 1;
          const apiError = error instanceof ApiError ? error : null;
          const originalMessage = error instanceof Error ? error.message : "The upload could not be completed.";
          let message = originalMessage;
          if (sourceFormat !== null
            && sourceFormat !== "png"
            && createdUploadId !== null
            && createdJobId !== null) {
            const sourceLabel = sourceFormat === "jpeg" ? "JPEG" : "SVG";
            const cancellationKey = cancellationKeys.current.get(createdUploadId)
              ?? newIdempotencyKey("cancel-upload");
            cancellationKeys.current.set(createdUploadId, cancellationKey);
            try {
              const cancelled = await api.cancelUpload(createdUploadId, cancellationKey);
              if (cancelled.value.upload.upload_id !== createdUploadId
                || cancelled.value.upload.job_id !== createdJobId
                || cancelled.value.upload.status !== "cancelled") {
                throw new Error("The upload cancellation response is inconsistent.");
              }
              recoveryKeys.current.delete(createdUploadId);
              cancellationKeys.current.delete(createdUploadId);
              message = `${originalMessage} The upload reservation was cancelled. Re-select the original ${sourceLabel} to retry.`;
            } catch {
              message = `${originalMessage} Restart before retrying; the upload reservation will expire or be reconciled.`;
            }
            if (abort.signal.aborted || batchEpoch.current !== epoch) return;
          } else if (sourceFormat === "png" && createdUploadId !== null) {
            message = `${originalMessage} Open recovery to check or resume this exact PNG.`;
          }
          dispatchBatch({
            type: "item",
            id: item.id,
            changes: {
              phase: apiError?.status === 410 ? "expired" : "error",
              message,
              error: originalMessage,
              requestId: apiError?.requestId ?? null,
            },
          });
        } finally {
          batchFiles.current.delete(item.id);
        }
      }
      if (!abort.signal.aborted && batchEpoch.current === epoch) {
        dispatchBatch({ type: "finish", failed });
      }
    } finally {
      if (batchEpoch.current === epoch) {
        batchActiveRef.current = false;
        batchAbortRef.current = null;
        batchFiles.current.clear();
      }
    }
  }, [api]);

  const recover = useCallback(async (knownRecovery?: UploadRecovery): Promise<void> => {
    const uploadId = knownRecovery?.upload_id ?? stateRef.current.uploadId;
    if (uploadId === null) return;
    const epoch = ++operationEpoch.current;
    abortRef.current?.abort();
    const abort = new AbortController();
    abortRef.current = abort;
    if (knownRecovery !== undefined) {
      dispatch({ type: "intent", uploadId: knownRecovery.upload_id, jobId: knownRecovery.job_id, filename: knownRecovery.filename });
    }
    dispatch({ type: "phase", phase: "finalizing", message: "Checking whether the exact upload already arrived…" });
    try {
      const recovery = knownRecovery ?? (await api.getUpload(uploadId)).value;
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (recovery.upload_id !== uploadId) throw new Error("The upload recovery authority is inconsistent.");
      dispatch({ type: "intent", uploadId: recovery.upload_id, jobId: recovery.job_id, filename: recovery.filename });
      if (reconcileTerminalUpload(uploadId, recovery.status)) return;
      const keys = recoveryKeys.current.get(uploadId) ?? {
        authorize: newIdempotencyKey("reauthorize-upload"),
        complete: newIdempotencyKey("complete-upload"),
      };
      recoveryKeys.current.set(uploadId, keys);
      const completed = await api.completeUpload(uploadId, keys.complete);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (completed.value.upload.upload_id !== uploadId
        || completed.value.upload.job_id !== recovery.job_id) throw new Error("The upload recovery authority is inconsistent.");
      if (!reconcileTerminalUpload(uploadId, completed.value.upload.status)) {
        throw new Error("The upload is not complete yet.");
      }
    } catch (error) {
      if (operationEpoch.current !== epoch || (error instanceof DOMException && error.name === "AbortError")) return;
      const apiError = error instanceof ApiError ? error : null;
      dispatch({
        type: "failure",
        message: error instanceof Error ? error.message : "The upload could not be verified.",
        requestId: apiError?.requestId ?? null,
        expired: apiError?.status === 410,
      });
    }
  }, [api, reconcileTerminalUpload]);

  const resume = useCallback(async (file: File, recovery: UploadRecovery): Promise<void> => {
    const epoch = ++operationEpoch.current;
    abortRef.current?.abort();
    const abort = new AbortController();
    abortRef.current = abort;
    dispatch({ type: "intent", uploadId: recovery.upload_id, jobId: recovery.job_id, filename: recovery.filename });
    try {
      if (recovery.status !== "open") throw new Error("This upload is no longer open.");
      if (file.name !== recovery.filename || file.size !== recovery.size_bytes) {
        throw new Error(`Choose the original ${recovery.filename} file (${recovery.size_bytes.toLocaleString()} bytes).`);
      }
      dispatch({ type: "phase", phase: "hashing", message: "Verifying the re-selected artwork…" });
      const sha256 = await validateAndHashPng(file);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      const keys = recoveryKeys.current.get(recovery.upload_id) ?? {
        authorize: newIdempotencyKey("reauthorize-upload"),
        complete: newIdempotencyKey("complete-upload"),
      };
      recoveryKeys.current.set(recovery.upload_id, keys);
      dispatch({ type: "phase", phase: "creating_intent", message: "Issuing a new short-lived upload authorization…" });
      const authorization = await obtainFreshAuthorization(
        api,
        recovery.upload_id,
        recovery.job_id,
        keys,
        () => operationEpoch.current === epoch && !abort.signal.aborted,
      );
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (
        authorization === null
        || authorization.job_id !== recovery.job_id
        || authorization.content_sha256 !== sha256
        || authorization.size_bytes !== file.size
      ) {
        throw new Error("The re-selected file does not match the reserved artwork.");
      }
      dispatch({ type: "phase", phase: "uploading", message: "Uploading the re-selected artwork…" });
      await uploadToAuthorizedS3(file, authorization, (progress) => {
        if (operationEpoch.current === epoch) dispatch({ type: "progress", progress });
      }, abort.signal);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      dispatch({ type: "phase", phase: "finalizing", message: "Verifying the exact uploaded file…" });
      const completed = await api.completeUpload(recovery.upload_id, keys.complete);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (completed.value.upload.upload_id !== recovery.upload_id
        || completed.value.upload.job_id !== recovery.job_id
        || completed.value.upload.status !== "completed") throw new Error("The upload did not reach its completed state.");
      recoveryKeys.current.delete(recovery.upload_id);
      dispatch({ type: "phase", phase: "complete", message: "Artwork verified. Preparation has started." });
    } catch (error) {
      if (operationEpoch.current !== epoch || (error instanceof DOMException && error.name === "AbortError")) return;
      const apiError = error instanceof ApiError ? error : null;
      dispatch({
        type: "failure",
        message: error instanceof Error ? error.message : "The upload could not be resumed.",
        requestId: apiError?.requestId ?? null,
        expired: apiError?.status === 410,
      });
    }
  }, [api]);

  const cancel = useCallback(async (): Promise<void> => {
    const uploadId = stateRef.current.uploadId;
    const jobId = stateRef.current.jobId;
    const epoch = ++operationEpoch.current;
    abortRef.current?.abort();
    const abort = new AbortController();
    abortRef.current = abort;
    if (uploadId === null) {
      dispatch({ type: "reset" });
      return;
    }
    dispatch({ type: "phase", phase: "cancelling", message: "Cancelling the reserved upload…" });
    try {
      const idempotencyKey = cancellationKeys.current.get(uploadId) ?? newIdempotencyKey("cancel-upload");
      cancellationKeys.current.set(uploadId, idempotencyKey);
      const cancelled = await api.cancelUpload(uploadId, idempotencyKey);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (cancelled.value.upload.upload_id !== uploadId
        || jobId === null
        || cancelled.value.upload.job_id !== jobId
        || cancelled.value.upload.status !== "cancelled") {
        throw new Error("The upload cancellation response is inconsistent.");
      }
      cancellationKeys.current.delete(uploadId);
      dispatch({ type: "phase", phase: "cancelled", message: "Upload cancelled." });
    } catch (error) {
      if (operationEpoch.current !== epoch || (error instanceof DOMException && error.name === "AbortError")) return;
      const apiError = error instanceof ApiError ? error : null;
      dispatch({
        type: "failure",
        message: error instanceof Error ? error.message : "The upload could not be cancelled.",
        requestId: apiError?.requestId ?? null,
        expired: apiError?.status === 410,
      });
    }
  }, [api]);

  const reset = useCallback(() => {
    operationEpoch.current += 1;
    abortRef.current?.abort();
    batchEpoch.current += 1;
    batchAbortRef.current?.abort();
    batchAbortRef.current = null;
    batchActiveRef.current = false;
    batchFiles.current.clear();
    preIntentBeginActive.current = null;
    createKey.current = null;
    recoveryKeys.current.clear();
    cancellationKeys.current.clear();
    dispatch({ type: "reset" });
    dispatchBatch({ type: "reset" });
  }, []);

  const value = useMemo(
    () => ({ state, batch, begin, beginBatch, resume, recover, cancel, reset }),
    [state, batch, begin, beginBatch, resume, recover, cancel, reset],
  );
  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>;
}

export function useUpload(): UploadContextValue {
  const value = useContext(UploadContext);
  if (value === null) throw new Error("UploadProvider is missing");
  return value;
}

function uploadReducer(state: UploadState, action: UploadAction): UploadState {
  switch (action.type) {
    case "begin":
      return { ...initialState, phase: "validating", message: "Checking artwork…" };
    case "phase":
      return { ...state, phase: action.phase, message: action.message };
    case "terminal":
      return {
        ...state,
        phase: action.phase,
        progress: action.phase === "complete" ? 100 : state.progress,
        message: action.message,
        requestId: null,
      };
    case "intent":
      return { ...state, uploadId: action.uploadId, jobId: action.jobId, filename: action.filename };
    case "progress":
      return { ...state, progress: action.progress };
    case "failure":
      return {
        ...state,
        phase: action.expired ? "expired" : "error",
        message: action.message,
        requestId: action.requestId,
      };
    case "reset":
      return initialState;
  }
}

function uploadBatchReducer(state: UploadBatchState, action: UploadBatchAction): UploadBatchState {
  switch (action.type) {
    case "start":
      return {
        phase: "running",
        items: action.items,
        message: `Preparing ${action.items.length} artwork file${action.items.length === 1 ? "" : "s"} in order.`,
      };
    case "item":
      return {
        ...state,
        items: state.items.map((item) => item.id === action.id
          ? { ...item, ...action.changes }
          : item),
      };
    case "finish":
      return {
        ...state,
        phase: "complete",
        message: action.failed === 0
          ? `All ${state.items.length} artwork files started preparation.`
          : `${state.items.length - action.failed} of ${state.items.length} artwork files started preparation; ${action.failed} need attention.`,
      };
    case "failure":
      return { phase: "error", items: [], message: action.message };
    case "reset":
      return initialBatchState;
  }
}

interface UploadOperationKeys {
  authorize: string;
  complete: string;
}

async function createUploadWithNetworkReplay(
  api: ApiPort,
  file: File,
  sha256: string,
  idempotencyKey: string,
  isCurrent: () => boolean,
): ReturnType<ApiPort["createUpload"]> {
  try {
    return await api.createUpload(file, sha256, idempotencyKey);
  } catch (error) {
    if (!(error instanceof TypeError) || !isCurrent()) throw error;
    return api.createUpload(file, sha256, idempotencyKey);
  }
}

async function obtainFreshAuthorization(
  api: ApiPort,
  uploadId: string,
  expectedJobId: string,
  keys: UploadOperationKeys,
  isCurrent: () => boolean,
): Promise<NonNullable<Awaited<ReturnType<ApiPort["authorizeUpload"]>>["value"]["authorization"]>> {
  const response = await api.authorizeUpload(uploadId, keys.authorize);
  if (!isCurrent()) throw new DOMException("Upload superseded", "AbortError");
  if (response.value.upload.upload_id !== uploadId
    || response.value.upload.job_id !== expectedJobId
    || response.value.upload.status !== "open") {
    throw new Error("The upload authorization response is inconsistent.");
  }
  if (response.value.authorization !== null) {
    keys.authorize = newIdempotencyKey("reauthorize-upload");
    return response.value.authorization;
  }
  const recovery = (await api.getUpload(uploadId)).value;
  if (!isCurrent()) throw new DOMException("Upload superseded", "AbortError");
  if (recovery.upload_id !== uploadId || recovery.job_id !== expectedJobId || recovery.status !== "open") {
    throw new Error("The upload authorization could not be reconciled.");
  }
  if (recovery.authorization_expires_at !== null && Date.parse(recovery.authorization_expires_at) > Date.now()) {
    throw new Error("The prior private upload authorization is still active. Try again after it expires.");
  }
  keys.authorize = newIdempotencyKey("reauthorize-upload");
  const renewed = await api.authorizeUpload(uploadId, keys.authorize);
  if (!isCurrent()) throw new DOMException("Upload superseded", "AbortError");
  if (renewed.value.upload.upload_id !== uploadId
    || renewed.value.upload.job_id !== expectedJobId
    || renewed.value.upload.status !== "open") {
    throw new Error("The upload authorization response is inconsistent.");
  }
  if (renewed.value.authorization === null) {
    throw new Error("The upload service did not issue a fresh authorization.");
  }
  keys.authorize = newIdempotencyKey("reauthorize-upload");
  return renewed.value.authorization;
}
