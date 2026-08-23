import { useEffect, useId, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { useSessionStatus } from "../auth/use-session";
import type { JobSummary } from "../contracts";
import { useUpload } from "../upload/upload-context";

export function HomePage() {
  const { api, auth } = useAppDependencies();
  const status = useSessionStatus(auth.session);
  const location = useLocation();
  const navigate = useNavigate();
  const upload = useUpload();
  const inputId = useId();
  const preIntentBusy = upload.state.uploadId === null
    && ["validating", "hashing", "creating_intent"].includes(upload.state.phase);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [jobsError, setJobsError] = useState<string | null>(null);

  useEffect(() => {
    if (status !== "authenticated") return;
    let active = true;
    void api.listJobs().then((response) => {
      if (active) setJobs(response.value.jobs);
    }).catch((error: unknown) => {
      if (active) setJobsError(error instanceof Error ? error.message : "Recent work is unavailable.");
    });
    return () => { active = false; };
  }, [api, status]);

  useEffect(() => {
    if (upload.state.uploadId !== null && upload.state.phase !== "complete") {
      void navigate(`/uploads/${upload.state.uploadId}`);
    }
  }, [navigate, upload.state.phase, upload.state.uploadId]);

  if (status === "anonymous") {
    return (
      <section className="page landing-page">
        <div>
          <p className="eyebrow">Private seller workspace</p>
          <h1>One artwork in. A complete listing ready for your decision.</h1>
          <p className="lede">Mr. Lister reviews a PNG, drafts the listing, stages an unpublished Printify product, and waits for you.</p>
          <button className="button button--primary" type="button" onClick={() => { void auth.startSignIn(location.pathname); }}>
            Sign in securely
          </button>
        </div>
        <ol className="process-list" aria-label="How Mr. Lister works">
          <li><span>01</span><strong>Upload one PNG</strong><small>Fingerprint-bound private transfer</small></li>
          <li><span>02</span><strong>Strands prepares it</strong><small>Agentic artwork and listing review</small></li>
          <li><span>03</span><strong>You decide</strong><small>Nothing is published to Etsy</small></li>
        </ol>
      </section>
    );
  }

  return (
    <div className="page dashboard-grid">
      <section className="hero-panel" aria-labelledby="upload-heading">
        <p className="eyebrow">New listing</p>
        <h1 id="upload-heading">Start with one artwork.</h1>
        <p>PNG only, up to 5 MB. Your browser fingerprints the exact file before its private upload.</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          const input = event.currentTarget.elements.namedItem("artwork");
          if (!(input instanceof HTMLInputElement) || input.files?.[0] === undefined) return;
          void upload.begin(input.files[0]);
        }}>
          <label className="drop-field" htmlFor={inputId} aria-disabled={preIntentBusy}>
            <span className="drop-icon" aria-hidden="true">↑</span>
            <strong>Choose PNG artwork</strong>
            <span>The original file is never put in browser storage.</span>
          </label>
          <input id={inputId} className="file-input" name="artwork" type="file" accept="image/png,.png" required disabled={preIntentBusy} />
          <button className="button button--primary" type="submit" disabled={preIntentBusy}>{preIntentBusy ? "Preparing…" : "Prepare listing"}</button>
          {upload.state.phase !== "idle" && <p className="loading-line" role="status" aria-live="polite">{upload.state.message}</p>}
        </form>
      </section>

      <section className="recent-panel" aria-labelledby="recent-heading">
        <div className="section-heading-row">
          <div>
            <p className="eyebrow">Your workspace</p>
            <h2 id="recent-heading">Recent preparations</h2>
          </div>
          <span className="count-chip">{jobs.length}</span>
        </div>
        {jobsError !== null && <p className="alert alert--error" role="alert">{jobsError}</p>}
        {jobs.length === 0 && jobsError === null ? (
          <div className="empty-state"><p>No preparations yet.</p><small>Your first upload will appear here.</small></div>
        ) : (
          <ul className="job-list">
            {jobs.map((job) => (
              <li key={job.job_id}>
                <Link to={`/jobs/${job.job_id}`}>
                  <span><strong>Open preparation</strong><small>{job.job_id} · Updated {formatDate(job.updated_at)}</small></span>
                  <span aria-hidden="true">→</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
