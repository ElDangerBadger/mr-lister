import { useEffect, useId, useRef, useState, type DragEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { useSessionStatus } from "../auth/use-session";
import type { JobSummary } from "../contracts";
import { WorkflowSteps } from "../components/WorkflowSteps";
import {
  MAX_BATCH_FILES,
  type BatchUploadItemState,
  useUpload,
} from "../upload/upload-context";
import {
  ARTWORK_FILE_INPUT_ACCEPT,
  artworkSourceFormatForFile,
} from "../upload/direct-upload";

export function HomePage() {
  const { api, auth } = useAppDependencies();
  const status = useSessionStatus(auth.session);
  const location = useLocation();
  const navigate = useNavigate();
  const upload = useUpload();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const batchBusy = upload.batch.phase === "running";
  const batchFinished = upload.batch.phase === "complete";
  const preIntentBusy = upload.state.uploadId === null
    && ["validating", "hashing", "creating_intent"].includes(upload.state.phase);
  const uploadLocked = preIntentBusy || batchBusy || batchFinished;
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [hideRecentJobs, setHideRecentJobs] = useState(false);

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

  useEffect(() => {
    if (!uploadLocked) return;
    dragDepthRef.current = 0;
    setDragActive(false);
  }, [uploadLocked]);

  const applySelection = (files: readonly File[]) => {
    if (files.length > MAX_BATCH_FILES) {
      setSelectedFiles([]);
      setSelectionError(`Choose no more than ${MAX_BATCH_FILES} files in one batch.`);
      return;
    }
    setSelectionError(null);
    setSelectedFiles([...files]);
  };

  const fileDrag = (event: DragEvent<HTMLElement>) => event.dataTransfer.types.includes("Files");
  const visibleJobs = hideRecentJobs ? [] : jobs;

  const clearRecentJobs = () => {
    setHideRecentJobs(true);
  };

  const restoreRecentJobs = () => {
    setHideRecentJobs(false);
  };

  if (status === "anonymous") {
    return (
      <section className="page landing-page">
        <div>
          <p className="eyebrow">From artwork to storefront</p>
          <h1>Your artwork.<br />Your next listing.</h1>
          <p className="lede">Turn a design into a listing you’re proud to publish. Mr. Lister prepares the copy, product mockups, and estimated proceeds. You make the final call.</p>
          <ol className="process-list" aria-label="How Mr. Lister works">
            <li><span>01</span><strong>Upload your artwork</strong><small>Start with a design you love.</small></li>
            <li><span>02</span><strong>Review and make it yours</strong><small>Edit the words. Check the product and costs.</small></li>
            <li><span>03</span><strong>Publish with confidence</strong><small>Confirm the exact listing before it goes to your store.</small></li>
          </ol>
        </div>
        <div className="panel signin-panel">
          <p className="eyebrow">Your workspace</p>
          <h2>Welcome to Mr. Lister.</h2>
          <p>Sign in to create your next listing or pick up where you left off.</p>
          <button className="button button--primary" type="button" onClick={() => { void auth.startSignIn(location.pathname); }}>
            Sign in securely
          </button>
          <p className="signin-note">Have an invitation to try Mr. Lister? Use the account provided with your invitation.</p>
          <p className="muted">Your drafts stay private until you approve and confirm publication.</p>
        </div>
      </section>
    );
  }

  return (
    <div className="page dashboard-grid">
      <WorkflowSteps current="Upload" />
      <section className="hero-panel" aria-labelledby="upload-heading">
        <p className="eyebrow">New listing</p>
        <h1 id="upload-heading">Let’s start with your artwork.</h1>
        <p>Upload a design, and we’ll prepare the listing, mockups, and costs for your review.</p>
        <details className="upload-requirements"><summary>PNG, SVG, or JPEG · Up to {MAX_BATCH_FILES} files · 5 MB each</summary>
          <p className="format-note">Original files stay on your device. PNG bytes are preserved; compatible SVG and JPEG files are converted to PNG in your browser before upload. Proportions and backgrounds are preserved. SVG files must be self-contained, with no linked assets, text, filters, or animation.</p>
        </details>
        <form onSubmit={(event) => {
          event.preventDefault();
          if (selectedFiles.length === 0 || selectedFiles.length > MAX_BATCH_FILES) return;
          setSelectionError(null);
          void upload.beginBatch(selectedFiles);
        }}>
          <label
            className={dragActive ? "drop-field drop-field--active" : "drop-field"}
            htmlFor={inputId}
            aria-disabled={uploadLocked}
            data-drag-active={dragActive ? "true" : "false"}
            onDragEnter={(event) => {
              if (!fileDrag(event)) return;
              event.preventDefault();
              if (uploadLocked) return;
              dragDepthRef.current += 1;
              setDragActive(true);
            }}
            onDragOver={(event) => {
              if (!fileDrag(event)) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = uploadLocked ? "none" : "copy";
            }}
            onDragLeave={(event) => {
              if (dragDepthRef.current < 1) return;
              event.preventDefault();
              dragDepthRef.current -= 1;
              if (dragDepthRef.current === 0) setDragActive(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              const carriesFiles = fileDrag(event);
              dragDepthRef.current = 0;
              setDragActive(false);
              if (uploadLocked || !carriesFiles) return;
              if (inputRef.current !== null) inputRef.current.value = "";
              applySelection([...event.dataTransfer.files]);
            }}
          >
            <span className="drop-icon" aria-hidden="true">↑</span>
            <strong>{dragActive ? "Drop artwork here" : "Drag and drop PNG, SVG, or JPEG artwork, or choose files"}</strong>
            <span>One design or a batch of up to five. Your proportions and backgrounds stay as you chose them.</span>
          </label>
          <input
            id={inputId}
            ref={inputRef}
            className="file-input"
            name="artwork"
            type="file"
            accept={ARTWORK_FILE_INPUT_ACCEPT}
            multiple
            disabled={uploadLocked}
            onChange={(event) => {
              const files = [...(event.currentTarget.files ?? [])];
              applySelection(files);
              if (files.length > MAX_BATCH_FILES) event.currentTarget.value = "";
            }}
          />
          {selectionError !== null && <p className="alert alert--error" role="alert">{selectionError}</p>}
          {selectedFiles.length > 0 && (upload.batch.phase === "idle" || upload.batch.phase === "error") && (
            <SelectedArtworkList files={selectedFiles} onChange={setSelectedFiles} />
          )}
          <button className="button button--primary" type="submit" disabled={uploadLocked || selectedFiles.length === 0}>
            {batchBusy
              ? "Uploading artwork…"
              : batchFinished
                ? "Uploads processed"
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

      <aside className="panel upload-guide" aria-labelledby="next-heading">
        <p className="eyebrow">A little help, all the way</p>
        <h2 id="next-heading">You create. We prepare.</h2>
        <ol className="process-list">
          <li><span>01</span><strong>A listing that fits your artwork</strong><small>Title, description, and tags ready for your edits.</small></li>
          <li><span>02</span><strong>See the finished product</strong><small>Review the original design and representative mockups together.</small></li>
          <li><span>03</span><strong>Know what you could earn</strong><small>Check estimated proceeds before making your decision.</small></li>
        </ol>
        <p className="quiet-note">You’ll review every listing before approving it. Publishing is a separate confirmation.</p>
      </aside>
      <section className="recent-panel" aria-labelledby="recent-heading">
        <div className="section-heading-row">
          <div>
            <p className="eyebrow">Your workspace</p>
            <h2 id="recent-heading">Recent preparations</h2>
          </div>
          <div className="section-heading-actions">
            <span className="count-chip">{visibleJobs.length}</span>
            {visibleJobs.length > 0 && (
              <button className="button button--quiet" type="button" onClick={clearRecentJobs}>
                Clear recent list
              </button>
            )}
            {hideRecentJobs && (
              <button className="button button--quiet" type="button" onClick={restoreRecentJobs}>
                Show recent list
              </button>
            )}
          </div>
        </div>
        {jobsError !== null && <p className="alert alert--error" role="alert">{jobsError}</p>}
        {visibleJobs.length === 0 && jobsError === null ? (
          <div className="empty-state">
            <p>{hideRecentJobs ? "Recent list cleared for now." : "No preparations yet."}</p>
            <small>
              {hideRecentJobs
                ? "This only hides the current view. Jobs, provider products, publication records, and audit history are preserved."
                : "Your first upload will appear here."}
            </small>
          </div>
        ) : (
          <ul className="job-list">
            {visibleJobs.map((job) => (
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
              <small>{formatBytes(file.size)} · {sourceFormatDescription(file)}</small>
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

function sourceFormatDescription(file: File): string {
  const sourceFormat = artworkSourceFormatForFile(file);
  if (sourceFormat === "svg") return "SVG converts locally";
  if (sourceFormat === "jpeg") return "JPEG converts locally";
  if (sourceFormat === "png") return "PNG preserved exactly";
  return "Unsupported file · this item will be rejected";
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
