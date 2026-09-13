import { useEffect, useState } from "react";
import type { SellerReview } from "../contracts";
import { isPreparationActive, preparationMilestones } from "../workflow";

export function PreparationProgress({ review }: { review: SellerReview }) {
  const preparing = isPreparationActive(review);
  const [longWait, setLongWait] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setLongWait(false);
    if (!preparing) return;
    setNow(Date.now());
    const timer = window.setTimeout(() => setLongWait(true), 30_000);
    const clock = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => {
      window.clearTimeout(timer);
      window.clearInterval(clock);
    };
  }, [preparing, review.job_id]);
  const elapsedSeconds = Math.max(0, Math.floor((now - Date.parse(review.created_at)) / 1_000));
  const activity = preparationActivity(review.stage);
  return (
    <section className="preparation-progress" aria-label="Preparation milestones">
      <ol className="milestones">
        {preparationMilestones(review).map(({ id, label, state }) => (
          <li key={id} className={`milestone milestone--${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="milestone-status">{state === "done" && <span aria-hidden="true">✓ </span>}{state === "done" ? "Complete" : state === "current" ? "In progress" : "Waiting"}</span>
            <strong>{label}</strong>
          </li>
        ))}
      </ol>
      {preparing && <div className="preparation-hint">
        <span className="preparation-hint-icon" aria-hidden="true">◷</span>
        <div className="preparation-hint-copy">
          <div className="preparation-hint-heading">
            <strong className="preparation-activity-label" role="status" aria-live="polite" aria-atomic="true">{activity.label}</strong>
            <span className="preparation-elapsed" role="timer" aria-live="off" aria-label="Elapsed time since submission">Since submission <time dateTime={`PT${elapsedSeconds}S`}>{formatElapsed(elapsedSeconds)}</time></span>
          </div>
          <p>{longWait ? "The first preparation after a quiet period may take a little longer. Your work is still being checked; you can keep this page open." : activity.detail}</p>
        </div>
      </div>}
    </section>
  );
}

function preparationActivity(stage: SellerReview["stage"]): { label: string; detail: string } {
  const detail = "Listing text becomes editable as it arrives. Mockups and costs will follow.";
  switch (stage) {
    case "artwork_review": return { label: "Reviewing your artwork", detail };
    case "listing_validation": return { label: "Writing and checking your listing", detail };
    case "product_sync": return { label: "Preparing your product previews", detail: "Product previews will appear here as they arrive." };
    case "economics_refresh": return { label: "Checking costs and shipping", detail: "We’re checking product costs and shipping before the final review." };
    default: return { label: "Preparing your listing", detail };
  }
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = String(seconds % 60).padStart(2, "0");
  return minutes < 60 ? `${minutes}:${remainder}` : `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${remainder}`;
}
