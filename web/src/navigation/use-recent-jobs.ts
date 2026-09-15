import { useCallback, useEffect, useRef, useState } from "react";
import { newIdempotencyKey, type ApiPort } from "../api/client";
import type { AuthSession, SessionStatus } from "../auth/session";
import type { JobSummary } from "../contracts";

const MAX_EMPTY_PAGES = 4;

/** Recent history belongs to the account; browser state only tracks pending requests. */
export function useRecentJobs(api: Pick<ApiPort, "listJobs" | "clearRecentJobs">, session: AuthSession, status: SessionStatus, refresh: boolean) {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [clearError, setClearError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [cleared, setCleared] = useState(false);
  const epoch = useRef(0);
  const readSequence = useRef(0);
  const clearPending = useRef(false);
  const clearKey = useRef<string | null>(null);

  useEffect(() => {
    epoch.current += 1;
    clearPending.current = false;
    clearKey.current = null;
    setJobs([]);
    setError(null);
    setClearError(null);
    setNextCursor(null);
    setCleared(false);
    setClearing(false);
    setLoading(false);
    return () => { epoch.current += 1; };
  }, [api, session, status]);

  const load = useCallback(async (cursor?: string) => {
    if (status !== "authenticated" || session.getStatus() !== "authenticated") return;
    const generation = epoch.current;
    const sequence = ++readSequence.current;
    const current = () => generation === epoch.current && sequence === readSequence.current
      && session.getStatus() === "authenticated";
    setLoading(true);
    setError(null);
    let next = cursor;
    const seen = new Set(cursor === undefined ? [] : [cursor]);
    try {
      for (let page = 0; page < MAX_EMPTY_PAGES; page += 1) {
        const response = await api.listJobs(next);
        if (!current()) return;
        const { jobs: found, next_cursor: continuation } = response.value;
        if (continuation !== null && seen.has(continuation)) {
          throw new Error("Recent work could not be loaded. Please try again.");
        }
        if (continuation !== null) seen.add(continuation);
        // Cleared rows can occupy an entire backend page. Follow a small bounded
        // number, then leave an explicit continuation instead of hiding newer work.
        if (found.length > 0 || continuation === null || page === MAX_EMPTY_PAGES - 1) {
          setJobs((previous) => [...new Map(
            [...(cursor === undefined ? [] : previous), ...found].map((job) => [job.job_id, job]),
          ).values()]);
          setNextCursor(continuation);
          return;
        }
        next = continuation;
      }
    } catch (reason) {
      if (current()) setError(reason instanceof Error ? reason.message : "Recent work is unavailable.");
    } finally {
      if (current()) setLoading(false);
    }
  }, [api, session, status]);

  useEffect(() => { void load(); }, [load, refresh]);

  const clear = async () => {
    if (clearPending.current || status !== "authenticated" || session.getStatus() !== "authenticated") return;
    const generation = epoch.current;
    const current = () => generation === epoch.current && session.getStatus() === "authenticated";
    clearPending.current = true;
    clearKey.current ??= newIdempotencyKey("clear-recent");
    setClearing(true);
    setClearError(null);
    try {
      await api.clearRecentJobs(clearKey.current);
      if (!current()) return;
      clearKey.current = null;
      readSequence.current += 1;
      setJobs([]);
      setNextCursor(null);
      setCleared(true);
      await load();
    } catch {
      if (current()) setClearError("We couldn’t confirm that your recent list was cleared. Try again.");
    } finally {
      if (current()) {
        clearPending.current = false;
        setClearing(false);
      }
    }
  };

  return { jobs, error, clearError, nextCursor, loading, clearing, cleared, load, clear };
}
