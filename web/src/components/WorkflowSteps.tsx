export function WorkflowSteps({ current }: { current: "Upload" | "Review" | "Publish" }) {
  const steps = ["Upload", "Review", "Publish"] as const;
  const index = steps.indexOf(current);
  return (
    <nav className="workflow" aria-label="Listing workflow">
      <ol>
        {steps.map((step, position) => (
          <li key={step} className={position < index ? "workflow-done" : position === index ? "workflow-current" : ""} aria-current={position === index ? "step" : undefined}>
            <span className="workflow-number" aria-hidden="true">{position < index ? "✓" : position + 1}</span>
            <span>{step}</span>
            {position < index && <span className="visually-hidden"> complete</span>}
          </li>
        ))}
      </ol>
      <span className="workflow-note">You have the final say.</span>
    </nav>
  );
}
