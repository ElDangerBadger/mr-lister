import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, ContractError, newIdempotencyKey, type ListingDraft } from "../api/client";
import { useAppDependencies } from "../app-context";
import { sellerActions, type SellerAction, type SellerReview } from "../contracts";
import { PublicationWorkspace } from "../publication/PublicationWorkspace";

const POLLING_STATES = new Set([
  "preparing", "synchronizing", "refreshing_estimate", "reconciling", "cancelling",
]);

type SaveState = "pristine" | "dirty" | "saving" | "saved" | "conflict" | "error";
interface ReviewMinimum { recordVersion: number; reviewVersion: number }

export function JobReviewPage() {
  const { jobId = "" } = useParams();
  const { api, publicationApi } = useAppDependencies();
  const [loadedReview, setReview] = useState<SellerReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null);
  const [stageAnnouncement, setStageAnnouncement] = useState("");
  const [loadedPreviewKey, setLoadedPreviewKey] = useState<string | null>(null);
  const [loadedMockupSetKey, setLoadedMockupSetKey] = useState<string | null>(null);
  const lastStage = useRef<string | null>(null);
  const requestSequence = useRef(0);
  const nextPollDelay = useRef(3_000);
  const reviewRef = useRef<SellerReview | null>(null);
  const lifecycle = useRef({ mounted: false, generation: 0 });
  const routeIdentity = useRef({ jobId, epoch: 0 });
  if (routeIdentity.current.jobId !== jobId) {
    routeIdentity.current = { jobId, epoch: routeIdentity.current.epoch + 1 };
    requestSequence.current += 1;
  }
  const routeEpoch = routeIdentity.current.epoch;
  const markPreviewAvailable = useCallback((key: string) => setLoadedPreviewKey(key), []);
  const markPreviewUnavailable = useCallback((key: string) => {
    setLoadedPreviewKey((current) => current === key ? null : current);
  }, []);
  const markMockupsAvailable = useCallback((key: string) => setLoadedMockupSetKey(key), []);
  const markMockupsUnavailable = useCallback((key: string) => {
    setLoadedMockupSetKey((current) => current === key ? null : current);
  }, []);

  const review = loadedReview?.job_id === jobId ? loadedReview : null;
  reviewRef.current = review;

  useEffect(() => {
    lifecycle.current = { mounted: true, generation: lifecycle.current.generation + 1 };
    return () => {
      lifecycle.current = { mounted: false, generation: lifecycle.current.generation + 1 };
      requestSequence.current += 1;
    };
  }, []);

  const load = useCallback(async (minimum?: ReviewMinimum): Promise<boolean> => {
    const lifecycleGeneration = lifecycle.current.generation;
    const sequence = ++requestSequence.current;
    try {
      const response = await api.getReview(jobId);
      if (!lifecycle.current.mounted
        || lifecycle.current.generation !== lifecycleGeneration
        || routeIdentity.current.epoch !== routeEpoch
        || routeIdentity.current.jobId !== jobId
        || sequence !== requestSequence.current
        || response.value.job_id !== jobId) return false;
      const meetsMinimum = minimum === undefined
        || (response.value.record_version >= minimum.recordVersion
          && response.value.review_version >= minimum.reviewVersion);
      if (meetsMinimum) {
        setReview((current) => {
          if (current !== null
            && current.job_id === response.value.job_id
            && response.value.record_version < current.record_version) return current;
          return response.value;
        });
        setError(null);
      }
      nextPollDelay.current = 3_000;
      return meetsMinimum;
    } catch (reason) {
      if (!lifecycle.current.mounted
        || lifecycle.current.generation !== lifecycleGeneration
        || routeIdentity.current.epoch !== routeEpoch
        || routeIdentity.current.jobId !== jobId
        || sequence !== requestSequence.current) return false;
      const requestId = reason instanceof ApiError || reason instanceof ContractError ? reason.requestId : null;
      setError({
        message: reason instanceof Error ? reason.message : "The review is temporarily unavailable.",
        requestId: requestId === "unavailable" ? null : requestId,
      });
      const retryAfter = reason instanceof ApiError ? reason.retryAfterSeconds : null;
      nextPollDelay.current = retryAfter === null
        ? Math.min(30_000, Math.max(3_000, nextPollDelay.current * 2))
        : Math.min(30_000, Math.max(1_000, retryAfter * 1_000));
      return false;
    } finally {
      if (lifecycle.current.mounted
        && lifecycle.current.generation === lifecycleGeneration
        && routeIdentity.current.epoch === routeEpoch
        && routeIdentity.current.jobId === jobId
        && sequence === requestSequence.current) setLoading(false);
    }
  }, [api, jobId, routeEpoch]);

  const refreshProgress = useCallback(async (): Promise<void> => {
    const lifecycleGeneration = lifecycle.current.generation;
    try {
      const response = await api.getJob(jobId);
      if (!lifecycle.current.mounted
        || lifecycle.current.generation !== lifecycleGeneration
        || routeIdentity.current.epoch !== routeEpoch
        || routeIdentity.current.jobId !== jobId) return;
      if (response.value.job_id !== jobId) throw new ContractError(response.requestId);
      const current = reviewRef.current;
      if (current === null
        || response.value.record_version !== current.record_version
        || response.value.review_version !== current.review_version
        || response.value.display_state !== current.display_state
        || response.value.stage !== current.stage) {
        await load();
        if (!lifecycle.current.mounted
          || lifecycle.current.generation !== lifecycleGeneration
          || routeIdentity.current.epoch !== routeEpoch
          || routeIdentity.current.jobId !== jobId) return;
      } else {
        setError(null);
        nextPollDelay.current = 3_000;
      }
    } catch (reason) {
      if (!lifecycle.current.mounted
        || lifecycle.current.generation !== lifecycleGeneration
        || routeIdentity.current.epoch !== routeEpoch
        || routeIdentity.current.jobId !== jobId) return;
      const requestId = reason instanceof ApiError || reason instanceof ContractError ? reason.requestId : null;
      setError({
        message: reason instanceof Error ? reason.message : "Preparation status is temporarily unavailable.",
        requestId: requestId === "unavailable" ? null : requestId,
      });
      const retryAfter = reason instanceof ApiError ? reason.retryAfterSeconds : null;
      nextPollDelay.current = retryAfter === null
        ? Math.min(30_000, Math.max(3_000, nextPollDelay.current * 2))
        : Math.min(30_000, Math.max(1_000, retryAfter * 1_000));
    }
  }, [api, jobId, load, routeEpoch]);

  useEffect(() => {
    setLoading(true);
    setReview(null);
    setError(null);
    lastStage.current = null;
    setLoadedPreviewKey(null);
    setLoadedMockupSetKey(null);
    void load();
  }, [load]);

  useEffect(() => {
    if (review === null || lastStage.current === review.stage) return;
    if (lastStage.current !== null) setStageAnnouncement(`Preparation moved to ${humanLabel(review.stage)}.`);
    lastStage.current = review.stage;
  }, [review]);

  useEffect(() => {
    if (review === null) return;
    const continuouslyPoll = POLLING_STATES.has(review.display_state);
    let active = true;
    let inFlight = false;
    let timeout: number | null = null;
    const schedule = () => {
      if (active && continuouslyPoll && document.visibilityState === "visible" && navigator.onLine) {
        timeout = window.setTimeout(() => {
          if (!active || inFlight || !navigator.onLine || document.visibilityState !== "visible") return;
          inFlight = true;
          void refreshProgress().finally(() => {
            inFlight = false;
            if (active) schedule();
          });
        }, nextPollDelay.current);
      }
    };
    const refreshOnFocus = () => {
      if (!active || inFlight || !navigator.onLine || document.visibilityState !== "visible") return;
      if (timeout !== null) window.clearTimeout(timeout);
      timeout = null;
      inFlight = true;
      void refreshProgress().finally(() => {
        inFlight = false;
        if (active) schedule();
      });
    };
    schedule();
    window.addEventListener("focus", refreshOnFocus);
    window.addEventListener("online", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnFocus);
    return () => {
      active = false;
      if (timeout !== null) window.clearTimeout(timeout);
      window.removeEventListener("focus", refreshOnFocus);
      window.removeEventListener("online", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnFocus);
    };
  }, [refreshProgress, review]);

  if (loading && review === null) {
    return <section className="page narrow-page"><h1>Preparing your review…</h1><p role="status">Loading the authoritative seller view.</p></section>;
  }
  if (review === null) {
    return (
      <section className="page narrow-page">
        <p className="eyebrow">Review unavailable</p>
        <h1>We could not open this preparation.</h1>
        {error !== null && <ErrorNotice {...error} />}
        <button className="button" type="button" onClick={() => { void load(); }}>Try again</button>
      </section>
    );
  }

  return (
    <div className="page review-page">
      <div className="review-title-row">
        <div>
          <p className="eyebrow">Seller review · {review.job_id}</p>
          <h1>{review.listing.title ?? "Listing preparation"}</h1>
          <p>Current stage: <strong>{humanLabel(review.stage)}</strong></p>
        </div>
        <div className={`stage-badge stage-badge--${review.display_state}`}>{humanLabel(review.display_state)}</div>
      </div>
      <p className="visually-hidden" aria-live="polite" aria-atomic="true">{stageAnnouncement}</p>
      {publicationApi === undefined && <p className="boundary-note">{review.authority_notice}</p>}
      {error !== null && <ErrorNotice {...error} />}
      {review.provider_outcome_unconfirmed && (
        <div className="alert alert--warning" role="alert">The Printify outcome is not confirmed. Actions are limited until reconciliation finishes.</div>
      )}
      {review.failure !== null && <FailureCard review={review} />}

      <section className="strands-card" aria-labelledby="strands-heading">
        <div className="strands-symbol" aria-hidden="true">S</div>
        <div>
          <p className="eyebrow">Agentic preparation evidence</p>
          <h2 id="strands-heading">Prepared with Strands Agents</h2>
          {review.strands.readiness === "ready" ? (
            <p>Strands orchestration recorded the prepared review through its bounded <code>record_prepared_review</code> tool.</p>
          ) : (
            <p>Strands Agents preparation evidence is {humanLabel(review.strands.readiness)}.</p>
          )}
        </div>
        <dl className="compact-facts">
          <div><dt>Framework</dt><dd>{review.strands.framework ?? "Pending"}</dd></div>
          <div><dt>Agent</dt><dd>{review.strands.agent_id ?? "Pending"}</dd></div>
          <div><dt>Review version</dt><dd>{review.strands.prepared_review_version ?? "—"}</dd></div>
          <div><dt>Correlation ID</dt><dd>{review.strands.correlation_id ?? "Pending"}</dd></div>
          <div><dt>Recorded tool</dt><dd>{review.strands.tool_calls.join(", ") || "Pending"}</dd></div>
          <div><dt>Completed at</dt><dd>{review.strands.completed_at === null ? "Pending" : formatDate(review.strands.completed_at)}</dd></div>
        </dl>
      </section>

      <div className="review-grid">
        <section className="panel artwork-panel" aria-labelledby="artwork-heading">
          <SectionHeader eyebrow="Source" heading="Artwork review" id="artwork-heading" readiness={review.artwork.readiness} />
          <ArtworkPreview
            review={review}
            onAvailable={markPreviewAvailable}
            onUnavailable={markPreviewUnavailable}
          />
          {review.artwork.readiness === "ready" && (
            <div className="artwork-notes">
              <h3>{review.artwork.subject}</h3>
              <TokenList label="Visual elements" values={review.artwork.visual_elements} />
              <TokenList label="Styles" values={review.artwork.styles} />
              <TokenList label="Themes" values={review.artwork.themes} />
              {review.artwork.visible_text.length > 0 && <TokenList label="Visible text" values={review.artwork.visible_text} />}
              {review.artwork.safety_notes.length > 0 && <TokenList label="Safety notes" values={review.artwork.safety_notes} warning />}
              {review.artwork.confidence !== null && <p><small>Interpretation confidence: {Math.round(review.artwork.confidence * 100)}%</small></p>}
            </div>
          )}
        </section>

        <ListingEditor review={review} reload={load} />
      </div>

      <section className="panel" aria-labelledby="product-heading">
        <SectionHeader eyebrow="Fixed production policy" heading="Printify draft configuration" id="product-heading" readiness={review.synchronization.readiness} />
        <dl className="fact-grid">
          <Fact label="Product" value={review.product_policy.product_name} />
          <Fact label="Provider" value={review.product_policy.provider_name} />
          <Fact label="Colors" value={review.product_policy.colors.join(", ")} />
          <Fact label="Sizes" value={review.product_policy.sizes.join(", ")} />
          <Fact label="Retail price" value={money(review.product_policy.retail_price_cents)} />
          <Fact label="Buyer shipping" value={money(review.product_policy.buyer_shipping_cents)} />
          <Fact label="Print placements" value={`${review.product_policy.placements.length} fixed placement${review.product_policy.placements.length === 1 ? "" : "s"}`} />
          <Fact label="Printify product ID" value={review.synchronization.product_id ?? "Pending"} />
          <Fact label="Synchronized review" value={review.synchronization.review_version === null ? "Pending" : String(review.synchronization.review_version)} />
          <Fact label="Synchronized at" value={review.synchronization.synchronized_at === null ? "Pending" : formatDate(review.synchronization.synchronized_at)} />
          <Fact label="Draft editability" value={review.synchronization.editable_draft === null ? "Pending" : review.synchronization.editable_draft ? "Editable Printify draft" : "Read-only provider state"} />
        </dl>
        <details>
          <summary>Review exact print placements</summary>
          {review.product_policy.placements.map((placement) => (
            <dl className="fact-grid" key={placement.group_id}>
              <Fact label="Group ID" value={placement.group_id} />
              <Fact label="Sizes" value={placement.sizes.join(", ")} />
              <Fact label="Position" value={placement.position} />
              <Fact label="Decoration" value={placement.decoration_method} />
              <Fact label="X / Y" value={`${placement.x} / ${placement.y}`} />
              <Fact label="Scale / angle" value={`${placement.scale} / ${placement.angle}°`} />
            </dl>
          ))}
        </details>
        <p className="boundary-note">Its current Printify editability is shown above. Approval alone does not send it to Etsy; publication requires a separate explicit confirmation.</p>
      </section>

      <MockupGallery
        key={mockupSetKey(review)}
        review={review}
        onAvailable={markMockupsAvailable}
        onUnavailable={markMockupsUnavailable}
      />
      <EconomicsTable review={review} />
      <ActionPanel
        review={review}
        reload={load}
        approvalEvidenceAvailable={review.preview.url !== null
          && loadedPreviewKey === previewEvidenceKey(review)
          && loadedMockupSetKey === mockupSetKey(review)}
      />
      {publicationApi !== undefined
        && review.display_state === "approved"
        && review.stage === "complete" && (
          <PublicationWorkspace jobId={jobId} approvedReview={review} api={publicationApi} />
      )}
      <p className="updated-note">Authoritative record {review.record_version} · Updated {formatDate(review.updated_at)}</p>
    </div>
  );
}

function ArtworkPreview({ review, onAvailable, onUnavailable }: {
  review: SellerReview;
  onAvailable: (url: string) => void;
  onUnavailable: (url: string) => void;
}) {
  const { api } = useAppDependencies();
  const [loadedPreview, setLoadedPreview] = useState<{ sourceUrl: string; objectUrl: string } | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const activeLoad = useRef<string | null>(null);
  const evidenceKey = previewEvidenceKey(review);
  useEffect(() => {
    const previewUrl = review.preview.url;
    if (review.preview.readiness !== "ready" || previewUrl === null) return;
    const abort = new AbortController();
    const loadIdentity = `${evidenceKey}:${attempt}`;
    activeLoad.current = loadIdentity;
    let active = true;
    let localUrl: string | null = null;
    onUnavailable(evidenceKey);
    void api.fetchArtwork(previewUrl, abort.signal).then((blob) => {
      if (!active) return;
      localUrl = URL.createObjectURL(blob);
      setLoadedPreview({ sourceUrl: previewUrl, objectUrl: localUrl });
      setFailed(false);
    }).catch((reason: unknown) => {
      if (active && !(reason instanceof DOMException && reason.name === "AbortError")) {
        setFailed(true);
        onUnavailable(evidenceKey);
      }
    });
    return () => {
      active = false;
      if (activeLoad.current === loadIdentity) activeLoad.current = null;
      abort.abort();
      if (localUrl !== null) URL.revokeObjectURL(localUrl);
      setLoadedPreview((current) => current?.sourceUrl === previewUrl ? null : current);
      onUnavailable(evidenceKey);
    };
  }, [api, attempt, evidenceKey, onUnavailable, review.preview.readiness, review.preview.url]);

  if (review.preview.readiness !== "ready") return <div className="preview-placeholder" role="status">Artwork preview is {humanLabel(review.preview.readiness).toLocaleLowerCase()}.</div>;
  if (failed) return <div className="preview-placeholder" role="status"><p>Artwork preview needs to be refreshed.</p><button className="button" type="button" onClick={() => { setFailed(false); setAttempt((current) => current + 1); }}>Retry artwork preview</button></div>;
  const objectUrl = loadedPreview?.sourceUrl === review.preview.url ? loadedPreview.objectUrl : null;
  if (objectUrl === null) return <div className="preview-placeholder" role="status">Authorizing the private artwork preview…</div>;
  const loadIdentity = `${evidenceKey}:${attempt}`;
  return <img
    key={loadIdentity}
    className="artwork-preview"
    src={objectUrl}
    alt="Original uploaded artwork for this seller review"
    onLoad={() => {
      if (activeLoad.current === loadIdentity) onAvailable(evidenceKey);
    }}
    onError={() => {
      if (activeLoad.current !== loadIdentity) return;
      setFailed(true);
      onUnavailable(evidenceKey);
    }}
  />;
}

function ListingEditor({ review, reload }: { review: SellerReview; reload: (minimum?: ReviewMinimum) => Promise<boolean> }) {
  const { api } = useAppDependencies();
  const [draft, setDraft] = useState<ListingDraft | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("pristine");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const saveKey = useRef<{ authority: string; value: string } | null>(null);
  const acceptedMinimum = useRef<ReviewMinimum | null>(null);
  const editAuthority = useRef<SellerReview | null>(null);
  const validationSummary = useRef<HTMLDivElement>(null);
  const focusValidationSummary = useRef(false);

  useEffect(() => {
    if (review.listing.readiness !== "ready") return;
    if (saveState === "saved") {
      if (acceptedMinimum.current !== null && !meetsMinimum(review, acceptedMinimum.current)) return;
      acceptedMinimum.current = null;
      editAuthority.current = null;
    } else if (saveState !== "pristine") return;
    setDraft({
      title: review.listing.title ?? "",
      description: review.listing.description ?? "",
      tags: [...review.listing.tags],
    });
    editAuthority.current = null;
    setSaveState("pristine");
  }, [review, saveState]);

  useEffect(() => {
    const authority = editAuthority.current;
    if (draft === null
      || authority === null
      || saveState === "pristine"
      || saveState === "saving"
      || saveState === "saved"
      || sameReviewAuthority(authority, review)
      || !authoritativeListingMatchesDraft(review, draft)) return;
    saveKey.current = null;
    acceptedMinimum.current = null;
    editAuthority.current = null;
    setDraft({
      title: review.listing.title ?? "",
      description: review.listing.description ?? "",
      tags: [...review.listing.tags],
    });
    setErrors({});
    setSaveState("pristine");
    setMessage("The newer authoritative review contains this exact revision. No second save was sent.");
  }, [draft, review, saveState]);

  useEffect(() => {
    if (focusValidationSummary.current && Object.keys(errors).length > 0) {
      focusValidationSummary.current = false;
      validationSummary.current?.focus();
    }
  }, [errors]);

  const capability = capabilityFor(review, "edit_listing");
  if (draft === null) {
    return (
      <section className="panel" aria-labelledby="listing-heading">
        <SectionHeader eyebrow="Listing" heading="Draft content" id="listing-heading" readiness={review.listing.readiness} />
        <p className="validation-result">Validation: {validationResultLabel(review)}</p>
        <p>Listing content will appear after artwork preparation.</p>
      </section>
    );
  }

  const change = (next: ListingDraft) => {
    if (editAuthority.current === null) editAuthority.current = review;
    setDraft(next);
    setSaveState("dirty");
    setMessage(null);
  };
  const reconcileAcceptedRevision = async (minimum: ReviewMinimum) => {
    const current = await reload(minimum);
    if (current) setMessage("Revision accepted. The authoritative review is current.");
    else setMessage("Revision accepted. The latest authoritative review is temporarily unavailable; your accepted text remains visible.");
  };
  const submit = async () => {
    const authorityReview = editAuthority.current ?? review;
    if (!sameReviewAuthority(authorityReview, review)) {
      setSaveState("conflict");
      setMessage("A newer authoritative review is available. Reapply this preserved revision deliberately before saving.");
      return;
    }
    const validation = validateDraft(draft);
    focusValidationSummary.current = Object.keys(validation).length > 0;
    setErrors(validation);
    if (Object.keys(validation).length > 0) {
      setSaveState("error");
      setMessage("Correct the highlighted fields before saving.");
      return;
    }
    const normalizedDraft = {
      title: draft.title.trim(),
      description: draft.description.trim(),
      tags: draft.tags.map((tag) => tag.trim()),
    };
    const authority = `${authorityReview.record_version}:${authorityReview.review_version}:${authorityReview.review_authority_etag ?? "none"}:${JSON.stringify(normalizedDraft)}`;
    if (saveKey.current?.authority !== authority) saveKey.current = { authority, value: newIdempotencyKey("revise-listing") };
    setSaveState("saving");
    setMessage("Saving your revision…");
    try {
      const response = await api.reviseListing(authorityReview, normalizedDraft, saveKey.current.value);
      const minimum = { recordVersion: response.value.record_version, reviewVersion: response.value.review_version };
      acceptedMinimum.current = minimum;
      saveKey.current = null;
      setSaveState("saved");
      setMessage("Revision accepted. Refreshing the authoritative review…");
      void reconcileAcceptedRevision(minimum);
    } catch (reason) {
      if (reason instanceof ApiError && reason.isConflict) {
        setSaveState("conflict");
        setMessage(`${reason.message} Your unsaved revision is preserved below.`);
        await reload();
      } else {
        setSaveState("error");
        setMessage(reason instanceof Error ? reason.message : "The revision could not be saved.");
        if (reason instanceof ApiError && reason.fields.length > 0) {
          focusValidationSummary.current = true;
          setErrors(Object.fromEntries(reason.fields.map((field) => [normalizeApiPath(field.path), field.message])));
        }
      }
    }
  };

  const mergedIssues = review.validation.issues;
  const authorityConflict = editAuthority.current !== null && !sameReviewAuthority(editAuthority.current, review);
  const displayedSaveState: SaveState = authorityConflict ? "conflict" : saveState;
  const displayedMessage = authorityConflict
    ? "A newer authoritative review is available. Your local revision is preserved; reapply it deliberately before saving."
    : message;
  const projectedFieldErrors = Object.fromEntries(
    mergedIssues.filter((issue) => issue.severity === "error").map((issue) => [normalizeApiPath(issue.path), issue.message]),
  );
  const fieldErrors = { ...projectedFieldErrors, ...errors };
  return (
    <section className="panel listing-panel" aria-labelledby="listing-heading">
      <SectionHeader eyebrow="Listing" heading="Draft content" id="listing-heading" readiness={review.listing.readiness} />
      <p className="validation-result">Validation: {validationResultLabel(review)}</p>
      {(Object.keys(errors).length > 0 || mergedIssues.length > 0) && (
        <div id="listing-errors" ref={validationSummary} className="validation-summary" role="alert" tabIndex={-1}>
          <h3>Review these listing details</h3>
          <ul>
            {Object.entries(errors).map(([path, text]) => <li key={path}><a href={`#${fieldId(path)}`}>{text}</a></li>)}
            {mergedIssues.map((issue) => <li key={`${issue.code}:${issue.path}`}><a href={`#${fieldId(issue.path)}`}>{issue.message}</a></li>)}
          </ul>
        </div>
      )}
      <form onSubmit={(event) => { event.preventDefault(); void submit(); }} noValidate>
        <label htmlFor="listing-title">Title <span>{draft.title.length}/140</span></label>
        <input id="listing-title" value={draft.title} maxLength={140} readOnly={!capability.enabled || saveState === "saved"} disabled={saveState === "saving"} aria-invalid={fieldErrors.title !== undefined} aria-describedby={fieldErrors.title === undefined ? undefined : "listing-title-error"} onChange={(event) => change({ ...draft, title: event.target.value })} />
        {fieldErrors.title !== undefined && <small id="listing-title-error" className="field-error">{fieldErrors.title}</small>}

        <label htmlFor="listing-description">Description <span>{draft.description.length}/100,000</span></label>
        <textarea id="listing-description" rows={12} value={draft.description} maxLength={100_000} readOnly={!capability.enabled || saveState === "saved"} disabled={saveState === "saving"} aria-invalid={fieldErrors.description !== undefined} aria-describedby={fieldErrors.description === undefined ? undefined : "listing-description-error"} onChange={(event) => change({ ...draft, description: event.target.value })} />
        {fieldErrors.description !== undefined && <small id="listing-description-error" className="field-error">{fieldErrors.description}</small>}

        <fieldset disabled={saveState === "saving"}>
          <legend>Search tags <span>Exactly 13 · 20 characters each</span></legend>
          <div className="tag-grid">
            {Array.from({ length: 13 }, (_, index) => {
              const path = `tags[${index}]`;
              return (
                <div key={path}>
                  <label htmlFor={`listing-tag-${index + 1}`}>Tag {index + 1}</label>
                  <input id={`listing-tag-${index + 1}`} value={draft.tags[index] ?? ""} maxLength={20} readOnly={!capability.enabled || saveState === "saved"} aria-invalid={fieldErrors[path] !== undefined} aria-describedby={fieldErrors[path] === undefined ? undefined : `listing-tag-${index + 1}-error`} onChange={(event) => {
                    const tags = Array.from({ length: 13 }, (_, tagIndex) => draft.tags[tagIndex] ?? "");
                    tags[index] = event.target.value;
                    change({ ...draft, tags });
                  }} />
                  {fieldErrors[path] !== undefined && <small id={`listing-tag-${index + 1}-error`} className="field-error">{fieldErrors[path]}</small>}
                </div>
              );
            })}
          </div>
        </fieldset>
        <div className="form-actions">
          <button className="button button--primary" type="submit" disabled={!capability.enabled || displayedSaveState === "saving" || displayedSaveState === "pristine" || displayedSaveState === "saved" || displayedSaveState === "conflict"}>
            {saveState === "saving" ? "Saving…" : "Save listing revision"}
          </button>
          <span className={`save-state save-state--${displayedSaveState}`} role="status" aria-live="polite">{displayedMessage}</span>
          {displayedSaveState === "conflict" && (
            <button className="button" type="button" onClick={() => { editAuthority.current = review; setSaveState("dirty"); setMessage("Revision reapplied to the latest review. Review it, then save deliberately."); }}>Reapply revision to latest review</button>
          )}
          {saveState === "saved" && acceptedMinimum.current !== null && (
            <button className="button" type="button" onClick={() => { if (acceptedMinimum.current !== null) void reconcileAcceptedRevision(acceptedMinimum.current); }}>Refresh authoritative review</button>
          )}
        </div>
        {!capability.enabled && <p className="capability-message">{capability.message}</p>}
      </form>
      {review.listing.audience.length > 0 && <TokenList label="Prepared audience" values={review.listing.audience} headingLevel={3} />}
    </section>
  );
}

function ActionPanel({ review, reload, approvalEvidenceAvailable }: { review: SellerReview; reload: (minimum?: ReviewMinimum) => Promise<boolean>; approvalEvidenceAvailable: boolean }) {
  const { api } = useAppDependencies();
  const [running, setRunning] = useState<SellerAction | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [pendingMinimum, setPendingMinimum] = useState<ReviewMinimum | null>(null);
  const [confirmApproval, setConfirmApproval] = useState(false);
  const dialogHeading = useRef<HTMLHeadingElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const approvalTrigger = useRef<HTMLButtonElement>(null);
  const actionStatus = useRef<HTMLParagraphElement>(null);
  const actionsHeading = useRef<HTMLHeadingElement>(null);
  const [triggerFocusRequest, setTriggerFocusRequest] = useState(0);
  const [statusFocusRequest, setStatusFocusRequest] = useState(0);
  const operationKeys = useRef(new Map<string, string>());

  useEffect(() => {
    const dialogElement = dialog.current;
    if (!confirmApproval || dialogElement === null) return;
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.setAttribute("open", "");
    dialogHeading.current?.focus();
    return () => {
      if (dialogElement.open && typeof dialogElement.close === "function") dialogElement.close();
    };
  }, [confirmApproval]);

  useEffect(() => {
    if (triggerFocusRequest === 0 || confirmApproval) return;
    const currentTrigger = approvalTrigger.current;
    if (currentTrigger !== null && !currentTrigger.disabled) currentTrigger.focus();
    else actionsHeading.current?.focus();
  }, [confirmApproval, triggerFocusRequest]);

  useEffect(() => {
    if (statusFocusRequest === 0 || confirmApproval) return;
    (actionStatus.current ?? actionsHeading.current)?.focus();
  }, [confirmApproval, statusFocusRequest]);

  useEffect(() => {
    if (pendingMinimum !== null && meetsMinimum(review, pendingMinimum)) {
      setPendingMinimum(null);
      setMessage("Action accepted. Authoritative status is current.");
    }
  }, [pendingMinimum, review]);

  const reconcileAcceptedAction = async (minimum: ReviewMinimum) => {
    const current = await reload(minimum);
    if (current) {
      setPendingMinimum(null);
      setMessage("Action accepted. Authoritative status is current.");
    } else {
      setMessage("Action accepted, but the latest status is unavailable. Refresh status before issuing another action.");
    }
  };

  const execute = async (action: Exclude<SellerAction, "edit_listing">) => {
    const keyId = `${action}:${review.record_version}:${review.review_version}`;
    const idempotencyKey = operationKeys.current.get(keyId) ?? newIdempotencyKey(action);
    operationKeys.current.set(keyId, idempotencyKey);
    setRunning(action);
    setMessage(null);
    setRequestId(null);
    try {
      const response = await api.runAction(review, action, idempotencyKey);
      const minimum = { recordVersion: response.value.record_version, reviewVersion: response.value.review_version };
      operationKeys.current.delete(keyId);
      setMessage(action === "approve_review" ? "Draft approved. It remains unpublished. Refreshing status…" : "Action accepted. Refreshing status…");
      setPendingMinimum(minimum);
      setRunning(null);
      if (action === "approve_review") setStatusFocusRequest((current) => current + 1);
      void reconcileAcceptedAction(minimum);
      return;
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "The action could not be completed.");
      if (reason instanceof ApiError && reason.requestId !== "unavailable") setRequestId(reason.requestId);
      if (reason instanceof ApiError && reason.isConflict) await reload();
      if (action === "approve_review") setStatusFocusRequest((current) => current + 1);
    } finally {
      setRunning(null);
      setConfirmApproval(false);
    }
  };

  const dismissApproval = () => {
    if (running !== null) return;
    setTriggerFocusRequest((current) => current + 1);
    setConfirmApproval(false);
  };

  return (
    <section className="panel action-panel" aria-labelledby="actions-heading">
      <p className="eyebrow">Human decision boundary</p>
      <h2 id="actions-heading" ref={actionsHeading} tabIndex={-1}>Available actions</h2>
      <p>Capabilities come from the current server projection. Disabled actions explain what must happen first.</p>
      <div className="action-grid">
        {(["refresh_economics", "retry_job", "cancel_job", "approve_review"] as const).map((action) => {
          const capability = capabilityFor(review, action);
          return (
            <div className="action-item" key={action}>
              <button
                ref={action === "approve_review" ? approvalTrigger : undefined}
                className={`button ${action === "approve_review" ? "button--primary" : action === "cancel_job" ? "button--danger" : ""}`}
                type="button"
                disabled={!capability.enabled || running !== null || pendingMinimum !== null || (action === "approve_review" && !approvalEvidenceAvailable)}
                onClick={() => {
                  if (action === "approve_review") {
                    setConfirmApproval(true);
                  }
                  else void execute(action);
                }}
              >
                {running === action ? "Working…" : actionLabel(action)}
              </button>
              <small>{action === "approve_review" && capability.enabled && !approvalEvidenceAvailable ? "Load the original artwork and all representative mockups before approval." : capability.message}</small>
            </div>
          );
        })}
      </div>
      {message !== null && <p ref={actionStatus} className="alert alert--info" role="status" tabIndex={-1}>{message}{requestId !== null && <> Support reference: {requestId}.</>}</p>}
      {pendingMinimum !== null && running === null && (
        <button className="button" type="button" onClick={() => { void reconcileAcceptedAction(pendingMinimum); }}>Refresh current status</button>
      )}
      {confirmApproval && (
          <dialog ref={dialog} className="confirmation-dialog" aria-labelledby="approval-title" aria-describedby="approval-description" onCancel={(event) => { event.preventDefault(); dismissApproval(); }} onKeyDown={(event) => { if (event.key === "Escape") dismissApproval(); }}>
            <p className="eyebrow">Final review decision</p>
            <h3 id="approval-title" ref={dialogHeading} tabIndex={-1}>Approve this draft?</h3>
            <p id="approval-description"><strong>Approval does not publish to Etsy.</strong> It records your decision on this exact review version.</p>
            <div className="form-actions">
              <button className="button button--primary" type="button" disabled={!approvalEvidenceAvailable || running !== null} onClick={() => { void execute("approve_review"); }}>{running === "approve_review" ? "Approving…" : "Approve draft — keep unpublished"}</button>
              <button className="button" type="button" disabled={running !== null} onClick={dismissApproval}>Go back</button>
            </div>
          </dialog>
      )}
    </section>
  );
}

function MockupGallery({ review, onAvailable, onUnavailable }: {
  review: SellerReview;
  onAvailable: (key: string) => void;
  onUnavailable: (key: string) => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const loadedUrls = useRef(new Set<string>());
  const setKey = mockupSetKey(review);
  useEffect(() => () => onUnavailable(setKey), [onUnavailable, setKey]);
  const recordLoaded = (url: string) => {
    loadedUrls.current.add(url);
    if (loadedUrls.current.size === review.mockups.items.length) onAvailable(setKey);
  };
  const retry = () => {
    loadedUrls.current.clear();
    setFailed(false);
    onUnavailable(setKey);
    setAttempt((current) => current + 1);
  };
  return (
    <section className="panel" aria-labelledby="mockups-heading">
      <SectionHeader eyebrow="Printify evidence" heading="Generated mockups" id="mockups-heading" readiness={review.mockups.readiness} />
      {review.mockups.items.length === 0 ? <p>Mockups are not ready for review.</p> : (
        <><ul className="mockup-grid">
          {review.mockups.items.map((mockup) => <li key={mockup.url}><img key={`${setKey}:${mockup.url}:${attempt}`} src={mockup.url} alt={mockup.alt_text} onLoad={() => recordLoaded(mockup.url)} onError={() => { setFailed(true); onUnavailable(setKey); }} /></li>)}
        </ul>
        {failed && <p className="alert alert--warning" role="status">One or more representative mockups could not be loaded. <button className="button" type="button" onClick={retry}>Retry mockup images</button></p>}</>
      )}
    </section>
  );
}

function EconomicsTable({ review }: { review: SellerReview }) {
  const economics = review.economics;
  const displayable = economics.readiness === "ready" || economics.readiness === "stale";
  return (
    <section className="panel" aria-labelledby="economics-heading">
      <SectionHeader eyebrow="Review estimate" heading="Estimated proceeds" id="economics-heading" readiness={economics.readiness} />
      {!displayable || economics.minimum_cents === null || economics.maximum_cents === null ? <p>Estimated proceeds are {humanLabel(economics.readiness).toLocaleLowerCase()}; no monetary estimate is presented.</p> : (
        <>
          <p className="economics-range"><strong>{money(economics.minimum_cents)}–{money(economics.maximum_cents)}</strong><span>estimated per item across {economics.variants.length} variants</span></p>
          <details>
            <summary>Review all {economics.variants.length} variant estimates</summary>
            <div className="table-scroll" tabIndex={0}>
              <table>
                <caption>Estimated proceeds by product color and size, in US dollars</caption>
                <thead><tr><th scope="col">Color</th><th scope="col">Size</th><th scope="col">Retail</th><th scope="col">Buyer shipping</th><th scope="col">Production</th><th scope="col">Production shipping</th><th scope="col">Fees</th><th scope="col">Estimated proceeds</th></tr></thead>
                <tbody>{economics.variants.map((variant) => (
                  <tr key={`${variant.color}:${variant.size}`}><th scope="row">{variant.color}</th><td>{variant.size}</td><td>{money(variant.retail_price_cents)}</td><td>{money(variant.buyer_shipping_cents)}</td><td>{money(variant.production_cost_cents)}</td><td>{money(variant.production_shipping_cents)}</td><td>{money(variant.marketplace_fees_cents)}</td><td><strong>{money(variant.estimated_proceeds_cents)}</strong></td></tr>
                ))}</tbody>
              </table>
            </div>
          </details>
          <dl className="evidence-list">
            <div><dt>Calculated at</dt><dd>{economics.calculated_at === null ? "Unavailable" : formatDate(economics.calculated_at)}</dd></div>
            <div><dt>Production cost</dt><dd>{economics.production_cost_source ?? "Unavailable"}</dd></div>
            <div><dt>Production cost observed</dt><dd>{economics.production_cost_observed_at === null ? "Unavailable" : formatDate(economics.production_cost_observed_at)}</dd></div>
            <div><dt>Shipping</dt><dd>{economics.production_shipping_source ?? "Unavailable"}</dd></div>
            <div><dt>Shipping observed</dt><dd>{economics.production_shipping_observed_at === null ? "Unavailable" : formatDate(economics.production_shipping_observed_at)}</dd></div>
            <div><dt>Marketplace fee policy</dt><dd>{economics.fee_policy_source ?? "Unavailable"} {economics.fee_policy_id !== null && `· ${economics.fee_policy_id}`} {economics.fee_policy_verified_on !== null && `(verified ${economics.fee_policy_verified_on})`}</dd></div>
            <div><dt>Fresh until</dt><dd>{economics.fresh_until === null ? "Unavailable" : formatDate(economics.fresh_until)}</dd></div>
          </dl>
          {economics.assumptions.length > 0 && <TokenList label="Estimate assumptions" values={economics.assumptions} headingLevel={3} />}
        </>
      )}
    </section>
  );
}

function FailureCard({ review }: { review: SellerReview }) {
  const failure = review.failure;
  if (failure === null) return null;
  return <div className={`alert ${failure.retryable ? "alert--warning" : "alert--error"}`} role="alert"><strong>{failure.retryable ? "Preparation paused" : "Preparation stopped"}</strong><p>{failure.message}</p><dl className="compact-facts"><div><dt>Failure code</dt><dd>{failure.code}</dd></div><div><dt>Stage</dt><dd>{humanLabel(failure.stage)}</dd></div><div><dt>Recovery</dt><dd>{failure.recovery === null ? "No recovery action available" : humanLabel(failure.recovery)}</dd></div></dl></div>;
}

function ErrorNotice({ message, requestId }: { message: string; requestId: string | null }) {
  return <div className="alert alert--error" role="alert"><p>{message}</p>{requestId !== null && <small>Support reference: {requestId}</small>}</div>;
}

function SectionHeader({ eyebrow, heading, id, readiness }: { eyebrow: string; heading: string; id: string; readiness: string }) {
  return <div className="section-heading-row"><div><p className="eyebrow">{eyebrow}</p><h2 id={id}>{heading}</h2></div><span className="readiness-chip">{humanLabel(readiness)}</span></div>;
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function TokenList({ label, values, warning = false, headingLevel = 4 }: { label: string; values: string[]; warning?: boolean; headingLevel?: 3 | 4 }) {
  const Heading = headingLevel === 3 ? "h3" : "h4";
  return <div><Heading>{label}</Heading><ul className={`token-list ${warning ? "token-list--warning" : ""}`}>{values.map((value) => <li key={value}>{value}</li>)}</ul></div>;
}

function capabilityFor(review: SellerReview, action: SellerAction) {
  const index = sellerActions.indexOf(action);
  const capability = review.actions[index];
  if (capability === undefined || capability.action !== action) throw new Error("Closed action projection is invalid");
  return capability;
}

function validateDraft(draft: ListingDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (draft.title.trim().length === 0) errors.title = "Enter a listing title.";
  if (draft.title.length > 140) errors.title = "Keep the title to 140 characters or fewer.";
  if (draft.description.trim().length === 0) errors.description = "Enter a listing description.";
  if (draft.description.length > 100_000) errors.description = "Keep the description to 100,000 characters or fewer.";
  if (draft.tags.length !== 13) errors["tags[0]"] = "Provide exactly 13 tags.";
  const seen = new Map<string, number>();
  Array.from({ length: 13 }, (_, index) => draft.tags[index] ?? "").forEach((tag, index) => {
    const path = `tags[${index}]`;
    const normalized = tag.trim().toLocaleLowerCase("en-US");
    if (normalized.length === 0) errors[path] = `Enter tag ${index + 1}.`;
    else if (tag.trim().length > 20) errors[path] = `Keep tag ${index + 1} to 20 characters or fewer.`;
    else if (seen.has(normalized)) errors[path] = `Tag ${index + 1} duplicates tag ${(seen.get(normalized) ?? 0) + 1}.`;
    else seen.set(normalized, index);
  });
  return errors;
}

function fieldId(path: string): string {
  if (path === "title" || path.endsWith(".title")) return "listing-title";
  if (path === "description" || path.endsWith(".description")) return "listing-description";
  const match = /tags\[(\d+)\]/u.exec(path);
  return match?.[1] === undefined ? "listing-heading" : `listing-tag-${Number(match[1]) + 1}`;
}

function normalizeApiPath(path: string): string {
  if (path.endsWith(".title")) return "title";
  if (path.endsWith(".description")) return "description";
  const tag = /tags\[(\d+)\]/u.exec(path);
  return tag?.[1] === undefined ? path : `tags[${tag[1]}]`;
}

function actionLabel(action: Exclude<SellerAction, "edit_listing">): string {
  return { approve_review: "Approve draft", cancel_job: "Cancel preparation", retry_job: "Retry preparation", refresh_economics: "Refresh estimate" }[action];
}

function humanLabel(value: string): string {
  return value.replaceAll("_", " ").replace(/^./u, (character) => character.toUpperCase());
}

function validationResultLabel(review: SellerReview): string {
  if (review.validation.readiness !== "ready") return humanLabel(review.validation.readiness);
  return review.validation.passed === true ? "Passed" : "Needs revision";
}

function money(cents: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function meetsMinimum(review: SellerReview, minimum: ReviewMinimum): boolean {
  return review.record_version >= minimum.recordVersion && review.review_version >= minimum.reviewVersion;
}

function sameReviewAuthority(left: SellerReview, right: SellerReview): boolean {
  return left.job_id === right.job_id
    && left.record_version === right.record_version
    && left.review_version === right.review_version
    && left.review_fingerprint === right.review_fingerprint
    && left.review_authority_etag === right.review_authority_etag;
}

function authoritativeListingMatchesDraft(review: SellerReview, draft: ListingDraft): boolean {
  if (review.listing.readiness !== "ready" || review.listing.title === null || review.listing.description === null) return false;
  const normalizedTags = draft.tags.map((tag) => tag.trim());
  return review.listing.title === draft.title.trim()
    && review.listing.description === draft.description.trim()
    && review.listing.tags.length === normalizedTags.length
    && review.listing.tags.every((tag, index) => tag === normalizedTags[index]);
}

function previewEvidenceKey(review: SellerReview): string {
  return JSON.stringify({
    jobId: review.job_id,
    reviewVersion: review.review_version,
    url: review.preview.url,
  });
}

function mockupSetKey(review: SellerReview): string {
  return review.mockups.readiness === "ready"
    ? JSON.stringify({ jobId: review.job_id, reviewVersion: review.review_version, urls: review.mockups.items.map((item) => item.url) })
    : `unavailable:${review.job_id}:${review.review_version}`;
}
