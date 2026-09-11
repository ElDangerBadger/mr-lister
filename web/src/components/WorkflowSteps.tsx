export function WorkflowSteps({ current }: { current: "Upload" | "Review" | "Publish" }) {
  const steps = ["Upload", "Review", "Publish"] as const;
  const descriptions = ["Add your artwork", "Make it yours", "Go live on Etsy"];
  const index = steps.indexOf(current);
  return (
    <nav className="workflow" aria-label="Listing workflow">
      <ol>
        {steps.map((step, position) => (
          <li key={step} className={position < index ? "workflow-done" : position === index ? "workflow-current" : ""} aria-current={position === index ? "step" : undefined}>
            <span className="workflow-number" aria-hidden="true">{position < index ? "✓" : position + 1}</span>
            <span className="workflow-label"><strong>{step}</strong><small>{descriptions[position]}</small></span>
            {position < index && <span className="visually-hidden"> complete</span>}
          </li>
        ))}
      </ol>
    </nav>
  );
}
