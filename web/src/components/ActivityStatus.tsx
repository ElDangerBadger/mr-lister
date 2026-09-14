import type { ReactNode } from "react";

/** Mount only while work is active; the label remains readable without motion. */
export function ActivityStatus({ children }: { children: ReactNode }) {
  return <span className="activity-status" role="status" aria-live="polite" aria-atomic="true">
    <span className="activity-status-dot" aria-hidden="true" />
    <span className="activity-status-label">{children}</span>
  </span>;
}
