import { createContext, useCallback, useContext, useMemo, useReducer, useRef, type ReactNode } from "react";
import { ApiError, newIdempotencyKey, type ApiPort } from "../api/client";
import type { UploadRecovery } from "../contracts";
import { uploadToAuthorizedS3, validateAndHashPng } from "./direct-upload";

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

type UploadAction =
  | { type: "begin" }
  | { type: "phase"; phase: UploadPhase; message: string }
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
  message: "Choose one PNG artwork file to begin.",
  requestId: null,
};

interface UploadContextValue {
  state: UploadState;
  begin(file: File): Promise<void>;
  resume(file: File, recovery: UploadRecovery): Promise<void>;
  recover(recovery?: UploadRecovery): Promise<void>;
  cancel(): Promise<void>;
  reset(): void;
}

const UploadContext = createContext<UploadContextValue | null>(null);

export function UploadProvider({ api, children }: { api: ApiPort; children: ReactNode }) {
  const [state, dispatch] = useReducer(uploadReducer, initialState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const abortRef = useRef<AbortController | null>(null);
  const recoveryKeys = useRef(new Map<string, UploadOperationKeys>());
  const cancellationKeys = useRef(new Map<string, string>());
  const createKey = useRef<{ fileIdentity: string; value: string } | null>(null);
  const preIntentBeginActive = useRef<symbol | null>(null);
  const operationEpoch = useRef(0);

  const begin = useCallback(async (file: File): Promise<void> => {
    if (preIntentBeginActive.current !== null) return;
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
      if (recovery.status === "completed") {
        recoveryKeys.current.delete(uploadId);
        dispatch({ type: "phase", phase: "complete", message: "Artwork verified. Preparation has started." });
        return;
      }
      if (recovery.status !== "open") throw new Error(`This upload is ${recovery.status} and cannot be completed.`);
      const keys = recoveryKeys.current.get(uploadId) ?? {
        authorize: newIdempotencyKey("reauthorize-upload"),
        complete: newIdempotencyKey("complete-upload"),
      };
      recoveryKeys.current.set(uploadId, keys);
      const completed = await api.completeUpload(uploadId, keys.complete);
      if (abort.signal.aborted || operationEpoch.current !== epoch) return;
      if (completed.value.upload.upload_id !== uploadId
        || completed.value.upload.job_id !== recovery.job_id
        || completed.value.upload.status !== "completed") throw new Error("The upload is not complete yet.");
      recoveryKeys.current.delete(uploadId);
      dispatch({ type: "phase", phase: "complete", message: "Artwork verified. Preparation has started." });
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
  }, [api]);

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
    preIntentBeginActive.current = null;
    createKey.current = null;
    recoveryKeys.current.clear();
    cancellationKeys.current.clear();
    dispatch({ type: "reset" });
  }, []);

  const value = useMemo(() => ({ state, begin, resume, recover, cancel, reset }), [state, begin, resume, recover, cancel, reset]);
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

interface UploadOperationKeys {
  authorize: string;
  complete: string;
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
