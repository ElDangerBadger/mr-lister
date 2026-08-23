import { useEffect, useId, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import type { UploadRecovery } from "../contracts";
import { useUpload } from "../upload/upload-context";

const cancellable = new Set(["validating", "hashing", "creating_intent", "uploading", "finalizing"]);

export function UploadPage() {
  const { uploadId = "" } = useParams();
  const { api } = useAppDependencies();
  const upload = useUpload();
  const fileId = useId();
  const [loadedRecovery, setRecovery] = useState<UploadRecovery | null>(null);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const validUploadId = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/u.test(uploadId);
  const recovery = loadedRecovery?.upload_id === uploadId ? loadedRecovery : null;
  const active = upload.state.uploadId === uploadId;
  const canReauthorize = recovery?.authorization_expires_at === null
    || (recovery?.authorization_expires_at !== undefined && Date.parse(recovery.authorization_expires_at) <= Date.now());

  useEffect(() => {
    if ((active && upload.state.phase !== "error") || !validUploadId) return;
    let mounted = true;
    setRecovery(null);
    setRecoveryError(null);
    void api.getUpload(uploadId).then((response) => {
      if (mounted && response.value.upload_id === uploadId) setRecovery(response.value);
    }).catch((reason: unknown) => {
      if (mounted) setRecoveryError(reason instanceof Error ? reason.message : "Upload recovery is unavailable.");
    });
    return () => { mounted = false; };
  }, [active, api, upload.state.phase, uploadId, validUploadId]);

  useEffect(() => {
    if (!active && recovery?.upload_id === uploadId && recovery.status === "open") void upload.recover(recovery);
  }, [active, recovery, upload, uploadId]);

  if (!validUploadId) {
    return (
      <section className="page narrow-page">
        <p className="eyebrow">Invalid upload route</p>
        <h1>This private upload cannot be opened.</h1>
        <p><Link className="button" to="/">Return to uploads</Link></p>
      </section>
    );
  }

  if (!active && recovery === null) {
    return (
      <section className="page narrow-page">
        <p className="eyebrow">Upload recovery</p>
        <h1>Resume this private upload.</h1>
        {recoveryError === null ? <p role="status">Checking the owner-scoped upload record…</p> : <div className="alert alert--error" role="alert">{recoveryError}</div>}
        {recoveryError !== null && <p><Link className="button" to="/">Return to uploads</Link></p>}
      </section>
    );
  }

  if (!active && recovery !== null) {
    if (recovery.status === "completed") {
      return <section className="page narrow-page"><p className="eyebrow">Upload recovered</p><h1>Artwork verification is complete.</h1><p><Link className="button button--primary" to={`/jobs/${recovery.job_id}`}>Open seller review</Link></p></section>;
    }
    if (recovery.status !== "open") {
      return <section className="page narrow-page"><p className="eyebrow">Upload {recovery.status}</p><h1>This upload cannot be resumed.</h1><p><Link className="button" to="/">Start a new upload</Link></p></section>;
    }
    return (
      <section className="page narrow-page">
        <p className="eyebrow">Upload recovery</p>
        <h1>Re-select {recovery.filename}.</h1>
        <div className="alert alert--info" role="note">For your privacy, the browser did not retain the file. Mr. Lister will accept only the exact reserved PNG.</div>
        {canReauthorize ? <form onSubmit={(event) => {
          event.preventDefault();
          const input = event.currentTarget.elements.namedItem("artwork");
          if (input instanceof HTMLInputElement && input.files?.[0] !== undefined) void upload.resume(input.files[0], recovery);
        }}>
          <label htmlFor={fileId}>Original PNG file</label>
          <input id={fileId} name="artwork" type="file" accept="image/png,.png" required />
          <div className="form-actions"><button className="button button--primary" type="submit">Verify and resume</button><Link className="button" to="/">Start over</Link></div>
        </form> : <div className="alert alert--info" role="status">The prior short-lived authorization is still active but is never stored. Completion was checked first; reload after {recovery.authorization_expires_at === null ? "it expires" : new Date(recovery.authorization_expires_at).toLocaleTimeString()} to re-select the file safely.</div>}
      </section>
    );
  }

  return (
    <section className="page narrow-page" aria-labelledby="upload-progress-heading">
      <p className="eyebrow">Private upload</p>
      <h1 id="upload-progress-heading">{upload.state.filename}</h1>
      <div className="progress-card">
        <div className="progress-track" aria-hidden="true"><span className="progress-value" data-progress={upload.state.progress} /></div>
        <progress max="100" value={upload.state.progress} aria-label="Artwork upload progress">{upload.state.progress}%</progress>
        <p role="status" aria-live="polite">{upload.state.message}</p>
        {upload.state.requestId !== null && <small>Support reference: {upload.state.requestId}</small>}
      </div>
      {cancellable.has(upload.state.phase) && (
        <button className="button button--danger" type="button" onClick={() => { void upload.cancel(); }}>Cancel upload</button>
      )}
      {upload.state.phase === "complete" && upload.state.jobId !== null && (
        <Link className="button button--primary" to={`/jobs/${upload.state.jobId}`}>Follow preparation</Link>
      )}
      {["error", "expired", "cancelled"].includes(upload.state.phase) && (
        <div className="form-actions">
          {upload.state.phase === "error" && <button className="button" type="button" onClick={() => { void upload.recover(); }}>Check completion status</button>}
          {upload.state.phase === "error"
            ? <button className="button button--danger" type="button" onClick={() => { void upload.cancel(); }}>Cancel reserved upload</button>
            : <button className="button" type="button" onClick={() => upload.reset()}>Start over</button>}
        </div>
      )}
      {upload.state.phase === "error" && recovery?.status === "open" && canReauthorize && (
        <form onSubmit={(event) => {
          event.preventDefault();
          const input = event.currentTarget.elements.namedItem("recovery-artwork");
          if (input instanceof HTMLInputElement && input.files?.[0] !== undefined) void upload.resume(input.files[0], recovery);
        }}>
          <label htmlFor={fileId}>If the file has not arrived, re-select {recovery.filename}</label>
          <input id={fileId} name="recovery-artwork" type="file" accept="image/png,.png" required />
          <button className="button button--primary" type="submit">Verify and transfer exact file</button>
        </form>
      )}
    </section>
  );
}
