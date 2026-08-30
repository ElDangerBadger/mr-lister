import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { useSessionStatus } from "../auth/use-session";
import type { JobSummary } from "../contracts";
import {
  MAX_BATCH_FILES,
  type BatchUploadItemState,
  useUpload,
} from "../upload/upload-context";

export function HomePage() {
  const { api, auth } = useAppDependencies();
  const status = useSessionStatus(auth.session);
  const location = useLocation();
  const navigate = useNavigate();
  const upload = useUpload();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const batchBusy = upload.batch.phase === "running";
  const batchFinished = upload.batch.phase === "complete";
  const preIntentBusy = upload.state.uploadId === null
    && ["validating", "hashing", "creating_intent"].includes(upload.state.phase);
  const uploadLocked = preIntentBusy || batchBusy || batchFinished;
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
    if (upload.batch.phase === "idle"
      && upload.state.uploadId !== null
      && upload.state.phase !== "complete") {
      void navigate(`/uploads/${upload.state.uploadId}`);
    }
  }, [navigate, upload.batch.phase, upload.state.phase, upload.state.uploadId]);

  if (status === "anonymous") {
    return (
      <section className="page landing-page">
        <div>
          <p className="eyebrow">Private seller workspace</p>
          <h1>Your artwork in. Listings ready for your decision.</h1>
          <p className="lede">Mr. Lister accepts PNG or compatible self-contained SVG artwork, prepares each listing independently, stages unpublished Printify products, and waits for you.</p>
          <button className="button button--primary" type="button" onClick={() => { void auth.startSignIn(location.pathname); }}>
            Sign in securely
          </button>
        </div>
        <ol className="process-list" aria-label="How Mr. Lister works">
          <li><span>01</span><strong>Add PNG or SVG artwork</strong><small>Up to {MAX_BATCH_FILES} files in one ordered queue</small></li>
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
        <h1 id="upload-heading">Prepare a batch of artwork.</h1>
        <p>Select up to {MAX_BATCH_FILES} PNG or compatible self-contained SVG files, then arrange their submission order. Each file creates its own private listing preparation.</p>
        <p className="format-note"><strong>SVG stays local:</strong> your browser converts supported shapes and gradients to PNG before fingerprinting or upload. Linked assets, text, filters, and animation are not accepted. Each source file may be up to 5 MB.</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          if (selectedFiles.length === 0 || selectedFiles.length > MAX_BATCH_FILES) return;
          setSelectionError(null);
          void upload.beginBatch(selectedFiles);
        }}>
          <label className="drop-field" htmlFor={inputId} aria-disabled={uploadLocked}>
            <span className="drop-icon" aria-hidden="true">↑</span>
            <strong>Choose PNG or SVG artwork</strong>
            <span>Select one file or build a batch. Originals are never put in browser storage.</span>
          </label>
          <input
            id={inputId}
            ref={inputRef}
            className="file-input"
            name="artwork"
            type="file"
            accept="image/png,image/svg+xml,.png,.svg"
            multiple
            required
            disabled={uploadLocked}
            onChange={(event) => {
              const files = [...(event.currentTarget.files ?? [])];
              if (files.length > MAX_BATCH_FILES) {
                setSelectedFiles([]);
                setSelectionError(`Choose no more than ${MAX_BATCH_FILES} files in one batch.`);
                event.currentTarget.value = "";
                return;
              }
              setSelectionError(null);
              setSelectedFiles(files);
            }}
          />
          {selectionError !== null && <p className="alert alert--error" role="alert">{selectionError}</p>}
          {selectedFiles.length > 0 && (upload.batch.phase === "idle" || upload.batch.phase === "error") && (
            <SelectedArtworkList files={selectedFiles} onChange={setSelectedFiles} />
          )}
          <button className="button button--primary" type="submit" disabled={uploadLocked || selectedFiles.length === 0}>
            {batchBusy
              ? "Preparing batch…"
              : batchFinished
                ? "Batch complete"
                : selectedFiles.length === 0
                  ? "Choose artwork to continue"
                  : selectedFiles.length === 1
                    ? "Prepare 1 listing"
                    : `Prepare ${selectedFiles.length} listings`}
          </button>
          {upload.batch.phase === "running" && <p className="loading-line" role="status" aria-live="polite">{upload.batch.message}</p>}
        </form>
        {upload.batch.items.length > 0 && (
          <BatchProgress
            items={upload.batch.items}
            message={upload.batch.message}
            onReset={() => {
              upload.reset();
              setSelectedFiles([]);
              if (inputRef.current !== null) inputRef.current.value = "";
            }}
          />
        )}
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

function SelectedArtworkList({ files, onChange }: { files: File[]; onChange: (files: File[]) => void }) {
  const move = (index: number, offset: -1 | 1) => {
    const destination = index + offset;
    if (destination < 0 || destination >= files.length) return;
    const next = [...files];
    const current = next[index];
    const other = next[destination];
    if (current === undefined || other === undefined) return;
    next[index] = other;
    next[destination] = current;
    onChange(next);
  };
  return (
    <section className="selection-panel" aria-labelledby="selection-heading">
      <div className="section-heading-row section-heading-row--compact">
        <h2 id="selection-heading">Submission order</h2>
        <span className="count-chip">{files.length}/{MAX_BATCH_FILES}</span>
      </div>
      <ol className="selection-list">
        {files.map((file, index) => (
          <li key={`${file.name}:${file.size}:${file.lastModified}:${index}`}>
            <span className="queue-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            <span className="queue-file">
              <strong>{file.name}</strong>
              <small>{formatBytes(file.size)} · {file.name.toLocaleLowerCase("en-US").endsWith(".svg") ? "SVG converts locally" : "PNG"}</small>
            </span>
            <span className="queue-order-controls">
              <button className="button button--quiet" type="button" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move ${file.name} earlier`}>↑</button>
              <button className="button button--quiet" type="button" disabled={index === files.length - 1} onClick={() => move(index, 1)} aria-label={`Move ${file.name} later`}>↓</button>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function BatchProgress({
  items,
  message,
  onReset,
}: {
  items: readonly BatchUploadItemState[];
  message: string;
  onReset: () => void;
}) {
  const running = items.some((item) => !["complete", "error", "expired"].includes(item.phase));
  return (
    <section className="batch-panel" aria-labelledby="batch-heading">
      <div className="section-heading-row section-heading-row--compact">
        <div>
          <p className="eyebrow">Batch status</p>
          <h2 id="batch-heading">{message}</h2>
        </div>
        <span className="count-chip">{items.filter((item) => item.phase === "complete").length}/{items.length}</span>
      </div>
      <ol className="upload-queue">
        {items.map((item) => <BatchProgressItem item={item} key={item.id} />)}
      </ol>
      {!running && <button className="button" type="button" onClick={onReset}>Choose another batch</button>}
    </section>
  );
}

function BatchProgressItem({ item }: { item: BatchUploadItemState }) {
  const failed = item.phase === "error" || item.phase === "expired";
  return (
    <li className={failed ? "upload-queue-item upload-queue-item--error" : "upload-queue-item"}>
      <span className="queue-number" aria-hidden="true">{String(item.position).padStart(2, "0")}</span>
      <span className="queue-file">
        <strong>{item.filename}</strong>
        {item.preparedFilename !== null && item.preparedFilename !== item.filename && <small>{item.preparedFilename} · converted locally</small>}
        <small>{item.message}</small>
        {item.requestId !== null && <small>Support reference: {item.requestId}</small>}
      </span>
      <span className={`queue-status queue-status--${item.phase}`}>{batchPhaseLabel(item.phase)}</span>
      {item.phase === "uploading" && <progress max="100" value={item.progress} aria-label={`${item.filename} upload progress`}>{item.progress}%</progress>}
      {item.phase === "complete" && item.jobId !== null && <Link className="button button--quiet queue-link" to={`/jobs/${item.jobId}`}>Open listing</Link>}
      {failed && item.sourceFormat === "png" && item.uploadId !== null && <Link className="button button--quiet queue-link" to={`/uploads/${item.uploadId}`}>Recover upload</Link>}
    </li>
  );
}

function batchPhaseLabel(phase: BatchUploadItemState["phase"]): string {
  return {
    queued: "Queued",
    validating: "Checking",
    hashing: "Fingerprinting",
    creating_intent: "Reserving",
    uploading: "Uploading",
    finalizing: "Verifying",
    complete: "Started",
    expired: "Expired",
    error: "Needs attention",
  }[phase];
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function formatBytes(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value / (1024 * 1024)) + " MB";
}
