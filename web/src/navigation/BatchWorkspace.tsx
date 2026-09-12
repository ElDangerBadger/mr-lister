import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAppDependencies } from "../app-context";
import { useSessionStatus } from "../auth/use-session";
import type { JobProgress, SellerReview } from "../contracts";
import { useUpload, type BatchUploadItemState } from "../upload/upload-context";
import { WorkspaceLink } from "./WorkspaceNavigation";

const PREPARING_STATES = new Set<JobProgress["display_state"]>([
  "preparing", "synchronizing", "refreshing_estimate", "reconciling", "cancelling",
]);
const READY_STATES = new Set<JobProgress["display_state"]>(["ready_for_review", "needs_revision"]);

interface BatchWorkspaceValue {
  items: readonly BatchUploadItemState[];
  progressByJob: Record<string, JobProgress>;
  filenameByJob: Record<string, string>;
  progressErrorByJob: Record<string, string>;
  autoOpenPending: boolean;
  updateFromReview: (review: SellerReview) => void;
}

const BatchWorkspaceContext = createContext<BatchWorkspaceValue | null>(null);
const EMPTY_WORKSPACE: BatchWorkspaceValue = {
  items: [], progressByJob: {}, filenameByJob: {}, progressErrorByJob: {}, autoOpenPending: false,
  updateFromReview: () => undefined,
};

export function BatchWorkspaceProvider({ children }: { children: ReactNode }) {
  const { api, auth } = useAppDependencies();
  const status = useSessionStatus(auth.session);
  const { batch } = useUpload();
  const location = useLocation();
  const navigate = useNavigate();
  const [progressByJob, setProgressByJob] = useState<Record<string, JobProgress>>({});
  const [filenameByJob, setFilenameByJob] = useState<Record<string, string>>({});
  const [progressErrorByJob, setProgressErrorByJob] = useState<Record<string, string>>({});
  const [autoOpenPending, setAutoOpenPending] = useState(false);
  const autoOpen = useRef({ batchKey: "", eligible: false });
  const progressRef = useRef(progressByJob);
  const sessionRef = useRef(status);
  const requests = useRef(new Map<string, symbol>());
  const refreshPolling = useRef<(() => void) | null>(null);
  progressRef.current = progressByJob;
  sessionRef.current = status;
  const batchKey = batch.items[0]?.id ?? "";
  const completedKey = JSON.stringify(batch.items
    .filter((item) => item.phase === "complete" && item.jobId !== null)
    .map((item) => item.jobId));

  const updateFromReview = useCallback((review: SellerReview) => {
    if (status !== "authenticated" || sessionRef.current !== "authenticated") return;
    const current = progressRef.current[review.job_id];
    if (current !== undefined && (current.record_version > review.record_version
      || (current.record_version === review.record_version && current.review_version >= review.review_version
        && current.display_state === review.display_state && current.stage === review.stage))) return;
    const progress: JobProgress = {
      contract_version: review.contract_version, job_id: review.job_id,
      record_version: review.record_version, review_version: review.review_version,
      display_state: review.display_state, stage: review.stage, authority_notice: review.authority_notice,
      actions: review.actions, failure: review.failure, provider_outcome_unconfirmed: review.provider_outcome_unconfirmed,
      created_at: review.created_at, updated_at: review.updated_at,
    };
    progressRef.current = { ...progressRef.current, [review.job_id]: progress };
    setProgressByJob(progressRef.current);
    setProgressErrorByJob((errors) => {
      if (errors[review.job_id] === undefined) return errors;
      const next = { ...errors };
      delete next[review.job_id];
      return next;
    });
    refreshPolling.current?.();
  }, [status]);

  useEffect(() => {
    if (status !== "authenticated") {
      autoOpen.current = { batchKey: "", eligible: false };
      setAutoOpenPending(false);
      setProgressByJob({});
      setFilenameByJob({});
      setProgressErrorByJob({});
      return;
    }
    if (autoOpen.current.batchKey !== batchKey) {
      autoOpen.current = {
        batchKey,
        eligible: batchKey !== "" && batch.phase === "running" && location.pathname === "/",
      };
    } else if (location.pathname !== "/") {
      autoOpen.current.eligible = false;
    }
    setAutoOpenPending(autoOpen.current.eligible);
  }, [batchKey, batch.phase, location.pathname, status]);

  useEffect(() => {
    if (status !== "authenticated") return;
    setFilenameByJob((current) => {
      const next = { ...current };
      for (const item of batch.items) {
        if (item.jobId !== null) next[item.jobId] = item.filename;
      }
      return next;
    });
  }, [batch.items, status]);

  useEffect(() => {
    if (status !== "authenticated") return;
    const jobIds = JSON.parse(completedKey) as string[];
    if (jobIds.length === 0) return;
    let active = true;
    let polling = false;
    let timeout: number | null = null;
    let delay = 3_000;
    const canPoll = () => document.visibilityState === "visible" && navigator.onLine;
    const pending = () => jobIds.filter((jobId) => {
      const progress = progressRef.current[jobId];
      return progress === undefined || PREPARING_STATES.has(progress.display_state);
    });
    const schedule = () => {
      if (active && canPoll() && pending().length > 0) timeout = window.setTimeout(() => { void poll(); }, delay);
    };
    const poll = async () => {
      if (!active || polling || !canPoll()) return;
      polling = true;
      let nextDelay = 3_000;
      await Promise.all(pending().map(async (jobId) => {
        if (requests.current.has(jobId)) return;
        const request = Symbol(jobId);
        requests.current.set(jobId, request);
        try {
          const response = await api.getJob(jobId);
          if (!active || sessionRef.current !== "authenticated" || response.value.job_id !== jobId) return;
          const current = progressRef.current[jobId];
          if (current !== undefined && response.value.record_version < current.record_version) return;
          progressRef.current = { ...progressRef.current, [jobId]: response.value };
          setProgressByJob(progressRef.current);
          setProgressErrorByJob((errors) => {
            const next = { ...errors };
            delete next[jobId];
            return next;
          });
        } catch (reason) {
          if (!active || sessionRef.current !== "authenticated") return;
          const retryAfter = reason instanceof ApiError ? reason.retryAfterSeconds : null;
          nextDelay = Math.max(nextDelay, retryAfter === null
            ? Math.min(30_000, delay * 2)
            : Math.min(30_000, Math.max(1_000, retryAfter * 1_000)));
          setProgressErrorByJob((errors) => ({
            ...errors,
            [jobId]: "Status is temporarily unavailable. We’ll check again shortly.",
          }));
        } finally {
          if (requests.current.get(jobId) === request) requests.current.delete(jobId);
        }
      }));
      polling = false;
      delay = nextDelay;
      schedule();
    };
    const refresh = () => {
      if (timeout !== null) window.clearTimeout(timeout);
      timeout = null;
      void poll();
    };
    refreshPolling.current = refresh;
    void poll();
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      active = false;
      if (refreshPolling.current === refresh) refreshPolling.current = null;
      if (timeout !== null) window.clearTimeout(timeout);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [api, completedKey, status]);

  useEffect(() => {
    if (status !== "authenticated" || location.pathname !== "/"
      || autoOpen.current.batchKey !== batchKey || !autoOpen.current.eligible) return;
    const ready = batch.items.find((item) => {
      if (item.phase !== "complete" || item.jobId === null) return false;
      const progress = progressByJob[item.jobId];
      return progress !== undefined && READY_STATES.has(progress.display_state);
    });
    if (ready?.jobId !== null && ready?.jobId !== undefined) {
      autoOpen.current.eligible = false;
      setAutoOpenPending(false);
      void navigate(`/jobs/${ready.jobId}`);
      return;
    }
    const finishedWithoutReview = batch.phase !== "running" && batch.items.length > 0
      && batch.items.every((item) => item.phase === "error" || item.phase === "expired"
        || (item.phase === "complete" && item.jobId !== null && progressByJob[item.jobId] !== undefined
          && !PREPARING_STATES.has(progressByJob[item.jobId]!.display_state)));
    if (finishedWithoutReview) {
      autoOpen.current.eligible = false;
      setAutoOpenPending(false);
    }
  }, [batch.items, batch.phase, batchKey, location.pathname, navigate, progressByJob, status]);

  return <BatchWorkspaceContext.Provider value={status === "authenticated"
    ? { items: batch.items, progressByJob, filenameByJob, progressErrorByJob, autoOpenPending, updateFromReview }
    : EMPTY_WORKSPACE}>
    {children}
  </BatchWorkspaceContext.Provider>;
}

export function useBatchWorkspace(): BatchWorkspaceValue {
  const value = useContext(BatchWorkspaceContext);
  return value ?? EMPTY_WORKSPACE;
}

export function batchItemStatus(item: BatchUploadItemState, progress?: Pick<JobProgress, "display_state">): string {
  if (item.phase === "complete" && progress !== undefined) return jobProgressLabel(progress);
  return {
    queued: "Queued", validating: "Checking artwork", hashing: "Checking artwork", creating_intent: "Starting upload",
    uploading: "Uploading artwork", finalizing: "Verifying artwork", complete: "Preparing listing",
    error: "Upload needs attention", expired: "Upload expired",
  }[item.phase];
}

export function jobProgressLabel(progress: Pick<JobProgress, "display_state">): string {
  return {
    preparing: "Preparing listing", needs_revision: "Ready to edit", synchronizing: "Preparing product",
    ready_for_review: "Ready for review", refreshing_estimate: "Updating estimate", reconciling: "Checking product",
    cancelling: "Cancelling", retryable_failure: "Needs attention", terminal_failure: "Preparation stopped",
    cancelled: "Cancelled", approved: "Review approved",
  }[progress.display_state];
}

export function BatchNavigator({ currentReview }: { currentReview?: SellerReview }) {
  const { items, progressByJob, progressErrorByJob, updateFromReview } = useBatchWorkspace();
  const { pathname } = useLocation();
  useEffect(() => {
    if (currentReview !== undefined && pathname === `/jobs/${currentReview.job_id}`) updateFromReview(currentReview);
  }, [currentReview, pathname, updateFromReview]);
  if (items.length === 0 || (!pathname.startsWith("/jobs/") && !pathname.startsWith("/uploads/"))) return null;
  const available = items.filter((item) => item.phase === "complete" && item.jobId !== null);
  const currentIndex = available.findIndex((item) => pathname === `/jobs/${item.jobId}`);
  const batchIndex = items.findIndex((item) => item.jobId !== null && pathname === `/jobs/${item.jobId}`);
  const previous = currentIndex > 0 ? available[currentIndex - 1] : undefined;
  const next = currentIndex >= 0 ? available[currentIndex + 1] : undefined;
  return <nav className="batch-navigation" aria-label="Listings in this batch">
    <details>
      <summary className="batch-navigation-summary">
        <strong>{batchIndex >= 0 ? `Listing ${batchIndex + 1} of ${items.length}` : "Your current batch"}</strong>
        <span>{items.length} artwork {items.length === 1 ? "file" : "files"} · View listings</span>
      </summary>
      <ol className="batch-navigation-list">
        {items.map((item) => {
          const current = item.jobId !== null && pathname === `/jobs/${item.jobId}`;
          const progress = currentReview?.job_id === item.jobId ? currentReview : item.jobId === null ? undefined : progressByJob[item.jobId];
          const destination = item.phase === "complete" && item.jobId !== null ? `/jobs/${item.jobId}`
            : ["error", "expired"].includes(item.phase) && item.sourceFormat === "png" && item.uploadId !== null ? `/uploads/${item.uploadId}` : null;
          return <li key={item.id} className="batch-navigation-item" data-current={current || undefined}>
            {destination === null ? <strong>{item.filename}</strong>
              : <WorkspaceLink to={destination} aria-current={current ? "page" : undefined}>{item.filename}</WorkspaceLink>}
            <span>{batchItemStatus(item, progress)}</span>
            {item.jobId !== null && progressErrorByJob[item.jobId] !== undefined && <small>{progressErrorByJob[item.jobId]}</small>}
          </li>;
        })}
      </ol>
    </details>
    {(previous !== undefined || next !== undefined) && <div className="batch-navigation-pager">
      {previous !== undefined && <WorkspaceLink className="button button--quiet" to={`/jobs/${previous.jobId}`} aria-label={`Previous listing: ${previous.filename}`}>← Previous listing</WorkspaceLink>}
      {next !== undefined && <WorkspaceLink className="button button--quiet" to={`/jobs/${next.jobId}`} aria-label={`Next listing: ${next.filename}`}>Next listing →</WorkspaceLink>}
    </div>}
  </nav>;
}
