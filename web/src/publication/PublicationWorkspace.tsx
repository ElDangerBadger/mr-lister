import { useCallback, useEffect, useRef, useState } from "react";
import type { SellerReview } from "../contracts";
import {
  PublicationApiError,
  PublicationContractError,
  type PublicationApiPort,
} from "./api-client";
import type { SellerPublicationProjection } from "./contracts";

const POLLING_STATES = new Set<SellerPublicationProjection["state"]>([
  "publication_requested",
  "publication_verifying",
  "publication_reconciling",
]);

interface PublicationWorkspaceProps {
  jobId: string;
  approvedReview: SellerReview;
  api: PublicationApiPort;
}

export function PublicationWorkspace({ jobId, approvedReview, api }: PublicationWorkspaceProps) {
  const [projection, setProjection] = useState<SellerPublicationProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [requestBlocked, setRequestBlocked] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const dialogHeading = useRef<HTMLHeadingElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const mounted = useRef(false);
  const authorityEpoch = useRef(0);
  const requestSequence = useRef(0);
  const idempotency = useRef<{ authority: string; value: string } | null>(null);
  const nextPollDelay = useRef(3_000);

  const approvalAuthority = [
    jobId,
    approvedReview.job_id,
    approvedReview.record_version,
    approvedReview.review_version,
    approvedReview.review_authority_etag ?? "missing",
  ].join(":");

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestSequence.current += 1;
    };
  }, []);

  useEffect(() => {
    authorityEpoch.current += 1;
    idempotency.current = null;
    setRequestBlocked(false);
    setMessage(null);
    setConfirming(false);
    setAcknowledged(false);
    setRequesting(false);
  }, [approvalAuthority]);

  const loadStatus = useCallback(async (): Promise<SellerPublicationProjection | null> => {
    const sequence = ++requestSequence.current;
    try {
      const response = await api.getPublication(jobId);
      if (!mounted.current || sequence !== requestSequence.current
        || response.value.job_id !== jobId) return null;
      setProjection((current) => {
        if (current !== null && current.job_id === response.value.job_id
          && current.aggregate_record_version !== null
          && response.value.aggregate_record_version !== null
          && response.value.aggregate_record_version < current.aggregate_record_version) return current;
        return response.value;
      });
      setError(null);
      nextPollDelay.current = 3_000;
      return response.value;
    } catch (reason) {
      if (!mounted.current || sequence !== requestSequence.current) return null;
      setError(reason instanceof Error ? reason.message : "Publication status is unavailable.");
      const retry = reason instanceof PublicationApiError ? reason.retryAfterSeconds : null;
      nextPollDelay.current = retry === null
        ? Math.min(30_000, Math.max(3_000, nextPollDelay.current * 2))
        : Math.min(30_000, Math.max(1_000, retry * 1_000));
      return null;
    } finally {
      if (mounted.current && sequence === requestSequence.current) setLoading(false);
    }
  }, [api, jobId]);

  useEffect(() => {
    setProjection(null);
    setLoading(true);
    setError(null);
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (projection === null || !POLLING_STATES.has(projection.state)) return;
    let active = true;
    let inFlight = false;
    let timeout: number | null = null;
    const schedule = () => {
      if (!active || !navigator.onLine || document.visibilityState !== "visible") return;
      timeout = window.setTimeout(() => {
        if (!active || inFlight || !navigator.onLine || document.visibilityState !== "visible") return;
        inFlight = true;
        void loadStatus().finally(() => {
          inFlight = false;
          if (active) schedule();
        });
      }, nextPollDelay.current);
    };
    const refresh = () => {
      if (!active || inFlight || !navigator.onLine || document.visibilityState !== "visible") return;
      if (timeout !== null) window.clearTimeout(timeout);
      timeout = null;
      inFlight = true;
      void loadStatus().finally(() => {
        inFlight = false;
        if (active) schedule();
      });
    };
    schedule();
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      active = false;
      if (timeout !== null) window.clearTimeout(timeout);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [loadStatus, projection]);

  useEffect(() => {
    const element = dialog.current;
    if (!confirming || element === null) return;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    dialogHeading.current?.focus();
    return () => {
      if (element.open && typeof element.close === "function") element.close();
    };
  }, [confirming]);

  const dismissConfirmation = () => {
    if (requesting) return;
    setAcknowledged(false);
    setConfirming(false);
    window.setTimeout(() => trigger.current?.focus(), 0);
  };

  const requestPublication = async () => {
    if (!acknowledged || requesting) return;
    const requestEpoch = authorityEpoch.current;
    const currentKey = idempotency.current;
    const key = currentKey?.authority === approvalAuthority
      ? currentKey.value
      : `web:publication:${approvedReview.record_version}:${approvedReview.review_version}:${crypto.randomUUID()}`;
    idempotency.current = { authority: approvalAuthority, value: key };
    setRequesting(true);
    setMessage(null);
    try {
      await api.requestPublication(approvedReview, key);
      if (!mounted.current || requestEpoch !== authorityEpoch.current) return;
      setRequestBlocked(true);
      setMessage("Publication request accepted. Checking authoritative status…");
      setConfirming(false);
      setAcknowledged(false);
      await loadStatus();
    } catch (reason) {
      if (!mounted.current || requestEpoch !== authorityEpoch.current) return;
      setRequestBlocked(true);
      setConfirming(false);
      setAcknowledged(false);
      const recovered = await loadStatus();
      if (!mounted.current || requestEpoch !== authorityEpoch.current) return;
      if (recovered !== null && recovered.state !== "not_requested") {
        setMessage("The authoritative publication request already exists. No second request was sent.");
      } else if (reason instanceof PublicationApiError && reason.isAuthorityConflict) {
        setMessage("The approved authority changed. Reload the approved listing before confirming again.");
      } else {
        setMessage("The request outcome is not confirmed. Check publication status; do not submit it again.");
      }
    } finally {
      if (mounted.current && requestEpoch === authorityEpoch.current) setRequesting(false);
    }
  };

  const approvedAuthorityCurrent = approvedReview.job_id === jobId
    && approvedReview.display_state === "approved"
    && approvedReview.stage === "complete"
    && approvedReview.review_version > 0
    && approvedReview.review_fingerprint !== null
    && approvedReview.review_authority_etag !== null;
  const mayRequest = projection?.state === "not_requested"
    && projection.request_enabled
    && approvedAuthorityCurrent
    && !requestBlocked;

  return (
    <section className="panel" aria-labelledby="phase7-publication-heading" data-phase7-publication-workspace="active">
      <p className="eyebrow">Etsy publication</p>
      <h2 id="phase7-publication-heading">Publication status</h2>
      {loading && projection === null && <p role="status">Loading authoritative publication status…</p>}
      {error !== null && <p className="alert alert--warning" role="alert">{error}</p>}
      {projection !== null && <PublicationStatus projection={projection} />}
      {message !== null && <p className="alert alert--info" role="status">{message}</p>}
      <div className="form-actions">
        {mayRequest && (
          <button ref={trigger} className="button button--primary" type="button" onClick={() => setConfirming(true)}>
            Publish this approved listing
          </button>
        )}
        <button className="button" type="button" disabled={requesting} onClick={() => { void loadStatus(); }}>
          Check publication status
        </button>
      </div>
      {projection?.state === "not_requested" && !projection.request_enabled && (
        <p>This approved listing is not eligible for publication.</p>
      )}
      {confirming && (
        <dialog
          ref={dialog}
          className="confirmation-dialog"
          aria-labelledby="phase7-confirmation-title"
          aria-describedby="phase7-confirmation-description"
          onCancel={(event) => { event.preventDefault(); dismissConfirmation(); }}
          onKeyDown={(event) => { if (event.key === "Escape") dismissConfirmation(); }}
        >
          <p className="eyebrow">Irreversible external action</p>
          <h3 id="phase7-confirmation-title" ref={dialogHeading} tabIndex={-1}>Publish this exact approved listing?</h3>
          <p id="phase7-confirmation-description">
            This can create a public Etsy listing. After the request is accepted, Mr. Lister cannot cancel, retry, or unpublish it.
          </p>
          <label>
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={requesting}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            I understand this is the one publication request for this approved listing.
          </label>
          <div className="form-actions">
            <button className="button button--danger" type="button" disabled={!acknowledged || requesting} onClick={() => { void requestPublication(); }}>
              {requesting ? "Requesting publication…" : "Publish exact approved listing"}
            </button>
            <button className="button" type="button" disabled={requesting} onClick={dismissConfirmation}>Go back</button>
          </div>
        </dialog>
      )}
    </section>
  );
}

function PublicationStatus({ projection }: { projection: SellerPublicationProjection }) {
  if (projection.state === "not_requested") {
    return <p>No publication request exists for this approved listing.</p>;
  }
  if (projection.state === "published") {
    return (
      <div>
        <p><strong>Published and positively verified.</strong></p>
        <p role="status">Your verified Etsy listing is ready.</p>
        <p><a href={projection.safe_listing_url ?? undefined} target="_blank" rel="noopener noreferrer">Open verified Etsy listing</a></p>
        <p>Immutable report reference: {projection.report_id}</p>
      </div>
    );
  }
  if (projection.state === "publication_outcome_unknown") {
    return (
      <div className="alert alert--warning" role="alert">
        <p><strong>Publication outcome could not be verified.</strong></p>
        <p>Do not retry this publication request. Use the immutable report reference for investigation: {projection.report_id}.</p>
      </div>
    );
  }
  if (projection.state === "publication_failed") {
    return (
      <div className="alert alert--warning" role="alert">
        <p><strong>Publication ended without a verified Etsy listing.</strong></p>
        <p>This one-shot request cannot be retried. Immutable report reference: {projection.report_id}.</p>
      </div>
    );
  }
  const messages: Record<Exclude<SellerPublicationProjection["stage"], "awaiting_activation" | "complete">, string> = {
    queued: "Publication is queued for its one bounded attempt.",
    preflight: "Checking the exact connected Etsy shop and approved product.",
    publishing: "The one authorized publication request is being processed.",
    verifying: "Printify accepted the request. Verifying the Etsy listing now.",
    reconciling: "The provider outcome may be uncertain. Read-only verification is continuing; do not retry.",
  };
  const stage = projection.stage;
  if (stage === "awaiting_activation" || stage === "complete") {
    throw new PublicationContractError("unavailable");
  }
  return (
    <div>
      <p role="status"><strong>{messages[stage]}</strong></p>
      <p>Verification deadline: {new Date(projection.verification_deadline ?? "").toLocaleString()}</p>
    </div>
  );
}
