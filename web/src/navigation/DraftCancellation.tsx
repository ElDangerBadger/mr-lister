import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAppDependencies } from "../app-context";
import { useSessionStatus } from "../auth/use-session";

export interface CanceledDraft {
  jobId: string;
  recordVersion: number;
  reviewVersion: number;
}

interface DraftCancellationFeedback {
  notice: boolean;
  canceledJobIds: ReadonlySet<string>;
  confirm: (draft?: CanceledDraft) => void;
  dismiss: () => void;
}

const EMPTY: DraftCancellationFeedback = {
  notice: false,
  canceledJobIds: new Set(),
  confirm: () => undefined,
  dismiss: () => undefined,
};
const DraftCancellationContext = createContext<DraftCancellationFeedback>(EMPTY);

/** Session-only feedback; only callers with a confirmed terminal receipt may mark a job. */
export function DraftCancellationProvider({ children }: { children: ReactNode }) {
  const { auth } = useAppDependencies();
  const status = useSessionStatus(auth.session);
  const [notice, setNotice] = useState(false);
  const [canceledJobIds, setCanceledJobIds] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    setNotice(false);
    setCanceledJobIds((current) => current.size === 0 ? current : new Set());
  }, [auth.session, status]);
  const confirm = useCallback((draft?: CanceledDraft) => {
    if (auth.session.getStatus() !== "authenticated") return;
    if (draft !== undefined) setCanceledJobIds((current) => current.has(draft.jobId) ? current : new Set(current).add(draft.jobId));
    setNotice(true);
  }, [auth.session]);
  const dismiss = useCallback(() => setNotice(false), []);
  const value = useMemo(() => ({ notice, canceledJobIds, confirm, dismiss }), [notice, canceledJobIds, confirm, dismiss]);
  return <DraftCancellationContext.Provider value={value}>{children}</DraftCancellationContext.Provider>;
}

export function useDraftCancellation() {
  return useContext(DraftCancellationContext);
}

export function DraftCanceledNotice() {
  const { notice, dismiss } = useDraftCancellation();
  const noticeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (notice) noticeRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [notice]);
  if (!notice) return null;
  return <div ref={noticeRef} className="alert alert--info draft-canceled-notice" role="status" aria-live="polite" aria-atomic="true">
    <div><h2>Draft Canceled</h2><p>Your draft was canceled.</p></div>
    <button className="button button--quiet" type="button" aria-label="Dismiss cancellation message" onClick={dismiss}>Dismiss</button>
  </div>;
}
