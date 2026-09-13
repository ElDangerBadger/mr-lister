import { useEffect, useId, useRef, useState, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { useSessionStatus } from "../auth/use-session";
import { LandingPage } from "./LandingPage";
import type { JobSummary } from "../contracts";
import { WorkflowSteps } from "../components/WorkflowSteps";
import { batchItemStatus, jobProgressLabel, useBatchWorkspace } from "../navigation/BatchWorkspace";
import { WorkspaceLink } from "../navigation/WorkspaceNavigation";
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
  const navigate = useNavigate();
  const upload = useUpload();
  const workspace = useBatchWorkspace();
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
  const showSelection = selectedFiles.length > 0 && (upload.batch.phase === "idle" || upload.batch.phase === "error");
  const showUploadQueue = showSelection || upload.batch.items.length > 0;
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [hideRecentJobs, setHideRecentJobs] = useState(false);

  useEffect(() => {
    if (status !== "authenticated") return;
    let active = true;
    void api.listJobs().then((response) => {
      if (active) {
        setJobs(response.value.jobs);
        setJobsError(null);
      }
    }).catch((error: unknown) => {
      if (active) setJobsError(error instanceof Error ? error.message : "Recent work is unavailable.");
    });
    return () => { active = false; };
  }, [api, status, batchFinished]);

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
    if (uploadLocked || files.length === 0) return;
    if (selectedFiles.length + files.length > MAX_BATCH_FILES) {
      const remaining = MAX_BATCH_FILES - selectedFiles.length;
      setSelectionError(`Choose no more than ${MAX_BATCH_FILES} files in one batch.${selectedFiles.length > 0 ? ` Your ${selectedFiles.length} selected files are still here; ${remaining === 0 ? "remove one before adding another" : `add up to ${remaining} more`}.` : ""}`);
      return;
    }
    setSelectionError(null);
    setSelectedFiles((current) => [...current, ...files]);
  };

  const changeSelection = (files: File[]) => {
    if (uploadLocked) return;
    setSelectedFiles(files);
    setSelectionError(null);
    if (files.length === 0) inputRef.current?.focus();
  };

  const fileDrag = (event: DragEvent<HTMLElement>) => event.dataTransfer.types.includes("Files");
  const visibleJobs = hideRecentJobs ? [] : jobs;

  const clearRecentJobs = () => {
    setHideRecentJobs(true);
  };

  const restoreRecentJobs = () => {
    setHideRecentJobs(false);
  };

  if (status === "anonymous") return <LandingPage />;

  return (
    <div className="page upload-page">
      <WorkflowSteps current="Upload" />
      <div className="review-title-row upload-heading">
        <div>
          <p className="eyebrow">New listing</p>
          <h1 id="upload-heading">Let’s start with your artwork.</h1>
          <p>Upload a design. We’ll prepare the listing for your review.</p>
        </div>
        <span className="status-pill">1 artwork = 1 listing</span>
      </div>
      <form className="upload-form" aria-labelledby="upload-heading" onSubmit={(event) => {
        event.preventDefault();
        if (uploadLocked || selectedFiles.length === 0 || selectedFiles.length > MAX_BATCH_FILES) return;
        setSelectionError(null);
        void upload.beginBatch(selectedFiles);
      }}>
        <div className="upload-grid">
          <section className="upload-main" aria-label="Choose your artwork">
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
              <span className="drop-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4" />
                </svg>
              </span>
              <strong>{dragActive ? "Drop artwork here" : "Drop your artwork here"}</strong>
              <span>PNG, SVG or JPEG · Up to {MAX_BATCH_FILES} files · 5 MB each</span>
              <span className="button button--primary upload-choose"><span aria-hidden="true">＋</span> Choose artwork</span>
              <span>Your design keeps its original proportions.</span>
              <span id={`${inputId}-label`} className="visually-hidden">Drag and drop PNG, SVG, or JPEG artwork, or choose files</span>
              <input
                id={inputId}
                aria-labelledby={`${inputId}-label`}
                ref={inputRef}
                className="file-input file-input--concealed"
                name="artwork"
                type="file"
                accept={ARTWORK_FILE_INPUT_ACCEPT}
                multiple
                disabled={uploadLocked}
                onChange={(event) => {
                  const files = [...(event.currentTarget.files ?? [])];
                  event.currentTarget.value = "";
                  applySelection(files);
                }}
              />
            </label>
            {selectionError !== null && <p className="alert alert--error" role="alert">{selectionError}</p>}
            <details className="upload-requirements">
              <summary>File requirements</summary>
              <p className="format-note">Original files stay on your device. PNG bytes are preserved; compatible SVG and JPEG files are converted to PNG in your browser before upload. Proportions and backgrounds are preserved. SVG files must be self-contained, with no linked assets, text, filters, or animation.</p>
            </details>
          </section>
          <aside className="panel upload-guide" aria-labelledby="next-heading">
            <h2 id="next-heading">From design to storefront.</h2>
            <ol className="process-list">
              <li><span>01</span><strong>Upload</strong><small>Add your finished artwork.</small></li>
              <li><span>02</span><strong>Review</strong><small>Edit the copy and check your product.</small></li>
              <li><span>03</span><strong>Publish</strong><small>You choose when it goes live.</small></li>
            </ol>
          </aside>
          {showUploadQueue && (
            <div className="upload-selection-column">
              {showSelection && (
                <SelectedArtworkList
                  files={selectedFiles}
                  onChange={changeSelection}
                  onAdd={() => { if (!uploadLocked) inputRef.current?.click(); }}
                  disabled={uploadLocked}
                />
              )}
              {upload.batch.items.length > 0 && (
                <BatchProgress
                  items={upload.batch.items}
                  message={upload.batch.message}
                  onReset={() => {
                    upload.reset();
                    setSelectedFiles([]);
                    setSelectionError(null);
                    if (inputRef.current !== null) inputRef.current.value = "";
                    inputRef.current?.focus();
                  }}
                />
              )}
            </div>
          )}
          <div className={showUploadQueue ? "upload-actionbar upload-actionbar--selected" : "upload-actionbar"}>
            <div>
              <strong>{batchBusy
                ? "Preparing your artwork"
                : batchFinished
                  ? "Follow your listings"
                  : selectedFiles.length === 0
                    ? "Your next listing starts here"
                    : `${selectedFiles.length} artwork ${selectedFiles.length === 1 ? "file" : "files"} selected`}
              </strong>
              <small>Nothing publishes until you approve and confirm.</small>
              {workspace.autoOpenPending && <p className="loading-line" role="status">{upload.batch.items.length === 1
                ? "Your listing will open after the artwork upload is verified. Preparation will continue there."
                : "Your first uploaded listing will open automatically. The rest will stay together in your batch."}</p>}
              {upload.batch.phase === "running" && <p className="loading-line" role="status" aria-live="polite">{upload.batch.message}</p>}
            </div>
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
          </div>
        </div>
      </form>
      <section className="recent-panel" aria-labelledby="recent-heading">
        <div className="section-heading-row">
          <div>
            <p className="eyebrow">Your workspace</p>
            <h2 id="recent-heading">Your listings</h2>
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
            <p>{hideRecentJobs ? "Recent list cleared for now." : "No listings yet."}</p>
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
                <WorkspaceLink to={`/jobs/${job.job_id}`} aria-label={`Open listing: ${workspace.filenameByJob[job.job_id] ?? job.job_id}`}>
                  <span className="job-list-label"><strong>{workspace.filenameByJob[job.job_id] ?? `Listing ${job.job_id.slice(-8)}`}</strong><small>Updated {formatDate(job.updated_at)}</small></span>
                  <span className="job-list-status">{(workspace.progressByJob[job.job_id]?.record_version ?? -1) >= job.record_version
                    ? jobProgressLabel(workspace.progressByJob[job.job_id]!) : jobStateLabel(job.state)}</span>
                  <span aria-hidden="true">→</span>
                </WorkspaceLink>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function SelectedArtworkList({ files, onChange, onAdd, disabled }: {
  files: File[];
  onChange: (files: File[]) => void;
  onAdd: () => void;
  disabled: boolean;
}) {
  const list = useRef<HTMLOListElement>(null);
  const removedIndex = useRef<number | null>(null);
  useEffect(() => {
    if (removedIndex.current === null) return;
    const controls = list.current?.querySelectorAll<HTMLButtonElement>(".file-remove");
    controls?.[Math.min(removedIndex.current, files.length - 1)]?.focus();
    removedIndex.current = null;
  }, [files]);

  const move = (index: number, offset: -1 | 1) => {
    if (disabled) return;
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
        <div className="section-heading-actions">
          <button className="button button--quiet" type="button" disabled={disabled || files.length >= MAX_BATCH_FILES} onClick={onAdd}>Add more artwork</button>
          <span className="count-chip">{files.length}/{MAX_BATCH_FILES}</span>
        </div>
      </div>
      <ol ref={list} className="selection-list">
        {files.map((file, index) => (
          <li key={`${file.name}:${file.size}:${file.lastModified}:${index}`}>
            <span className="queue-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            <span className="queue-file">
              <strong>{file.name}</strong>
              <small>{formatBytes(file.size)} · {sourceFormatDescription(file)}</small>
            </span>
            <span className="queue-order-controls">
              <button className="button button--quiet" type="button" disabled={disabled || index === 0} onClick={() => move(index, -1)} aria-label={`Move ${file.name} earlier`}>↑</button>
              <button className="button button--quiet" type="button" disabled={disabled || index === files.length - 1} onClick={() => move(index, 1)} aria-label={`Move ${file.name} later`}>↓</button>
              <button className="button button--quiet file-remove" type="button" disabled={disabled} onClick={() => {
                removedIndex.current = index;
                onChange(files.filter((_file, fileIndex) => fileIndex !== index));
              }} aria-label={`Remove ${file.name}`}>Remove</button>
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
  const { progressByJob, progressErrorByJob } = useBatchWorkspace();
  const progress = item.jobId === null ? undefined : progressByJob[item.jobId];
  const progressError = item.jobId === null ? undefined : progressErrorByJob[item.jobId];
  const failed = item.phase === "error" || item.phase === "expired";
  return (
    <li className={failed ? "upload-queue-item upload-queue-item--error" : "upload-queue-item"}>
      <span className="queue-number" aria-hidden="true">{String(item.position).padStart(2, "0")}</span>
      <span className="queue-file">
        <strong>{item.filename}</strong>
        {item.preparedFilename !== null && item.preparedFilename !== item.filename && <small>{item.preparedFilename} · converted locally</small>}
        <small>{item.phase === "complete" ? progressError ?? "Your artwork is uploaded. Open the listing at any time to follow its progress." : item.message}</small>
        {item.requestId !== null && <small>Support reference: {item.requestId}</small>}
      </span>
      <span className={`queue-status queue-status--${item.phase}`}>{batchItemStatus(item, progress)}</span>
      {item.phase === "uploading" && <progress max="100" value={item.progress} aria-label={`${item.filename} upload progress`}>{item.progress}%</progress>}
      {item.phase === "complete" && item.jobId !== null && <WorkspaceLink className="button button--quiet queue-link" to={`/jobs/${item.jobId}`}>Open listing</WorkspaceLink>}
      {failed && item.sourceFormat === "png" && item.uploadId !== null && <WorkspaceLink className="button button--quiet queue-link" to={`/uploads/${item.uploadId}`}>Recover upload</WorkspaceLink>}
    </li>
  );
}

function jobStateLabel(state: JobSummary["state"]): string {
  return {
    intake_validated: "Preparing listing", analyzing_artwork: "Preparing listing", listing_drafted: "Preparing listing",
    needs_revision: "Ready to edit", product_draft_syncing: "Preparing product", awaiting_approval: "Ready for review",
    pricing_refreshing: "Updating estimate", reconciliation_required: "Checking product", failed_retryable: "Needs attention",
    failed_terminal: "Preparation stopped", cancel_requested: "Cancelling", cancelled: "Cancelled", approved: "Review approved",
  }[state];
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function formatBytes(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value / (1024 * 1024)) + " MB";
}
