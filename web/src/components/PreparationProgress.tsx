import { useEffect, useState } from "react";
import type { SellerReview } from "../contracts";
import { preparationMilestones } from "../workflow";

export function PreparationProgress({ review }: { review: SellerReview }) {
  const preparing = !review.provider_outcome_unconfirmed
    && ["preparing", "synchronizing", "refreshing_estimate"].includes(review.display_state);
  const [longWait, setLongWait] = useState(false);
  useEffect(() => {
    setLongWait(false);
    if (!preparing) return;
    const timer = window.setTimeout(() => setLongWait(true), 30_000);
    return () => window.clearTimeout(timer);
  }, [preparing, review.job_id]);
  return (
    <section className="preparation-progress" aria-label="Preparation milestones">
      <ol className="milestones">
        {preparationMilestones(review).map(({ id, label, state }) => (
          <li key={id} className={`milestone milestone--${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="milestone-marker" aria-hidden="true">{state === "done" ? "✓" : state === "current" ? "•" : ""}</span>
            <span>{label}<small>{state === "done" ? "Complete" : state === "current" ? "In progress" : "Waiting"}</small></span>
          </li>
        ))}
      </ol>
      {preparing && <p className="preparation-hint">{longWait ? "The first preparation after a quiet period may take a little longer. Your work is still being checked; you can keep this page open." : "Listing text becomes editable as it arrives. Mockups and costs will follow."}</p>}
    </section>
  );
}
