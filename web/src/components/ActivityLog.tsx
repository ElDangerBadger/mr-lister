import { useEffect, useState, type ReactNode } from "react";
import type { SellerReview } from "../contracts";
import { preparationMilestones } from "../workflow";

interface ActivityEntry { key: string; at: string; label: string; completed: string }

export function ActivityLog({ review, children }: { review: SellerReview; children: ReactNode }) {
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const completed = preparationMilestones(review).filter((item) => item.state === "done").map((item) => item.label).join(" · ");
  const key = [review.display_state, review.stage, review.review_version, completed, review.provider_outcome_unconfirmed, review.failure?.code ?? ""].join(":");
  const label = `${review.display_state.replaceAll("_", " ")} · ${review.stage.replaceAll("_", " ")}${review.provider_outcome_unconfirmed ? " · awaiting provider confirmation" : ""}${review.failure !== null ? ` · ${review.failure.message}` : ""}`;
  useEffect(() => {
    setEntries((current) => current.at(-1)?.key === key ? current : [...current.slice(-19), { key, at: new Date().toISOString(), label, completed }]);
  }, [completed, key, label]);
  return (
    <details className="panel activity-panel">
      <summary>Activity &amp; details <span>Updates observed in this session</span></summary>
      <p className="muted">This log records changes while this review is open. It is not a full historical audit trail.</p>
      <ol className="activity-list">
        {entries.map((entry, index) => <li key={`${entry.key}:${index}`}><time dateTime={entry.at}>{new Date(entry.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time><div><strong>{entry.label}</strong><small>{entry.completed}</small></div></li>)}
      </ol>
      <dl className="compact-facts activity-reference"><div><dt>Preparation</dt><dd>{review.job_id}</dd></div><div><dt>Record / review version</dt><dd>{review.record_version} / {review.review_version}</dd></div><div><dt>Last server update</dt><dd>{new Date(review.updated_at).toLocaleString()}</dd></div></dl>
      {children}
    </details>
  );
}
