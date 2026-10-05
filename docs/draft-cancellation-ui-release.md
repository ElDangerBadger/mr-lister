# Draft wording and cancellation feedback

The October 5, 2026 frontend update uses “draft” in the upload/review flow instead of
“preparation.” Internal API actions, state names and backend behavior are unchanged.

Cancel appears beside Submit for an unsubmitted artwork selection. It clears only the local
selection and makes no API mutation. It is disabled while a batch is uploading; reset alone
cannot cancel server work. On the review page, Cancel draft is visible beside the primary
decision and respects server capability and saving barriers.

Submitted-draft cancellation remains an authenticated, idempotent `cancel_job` operation.
A matching terminal `cancelled` receipt, or a sufficiently current same-job terminal readback,
returns to the upload start route with a visible, accessible **Draft Canceled** notice. The
router replacement retains the judge basename. Pending/unknown cancellation shows explicit
confirmation activity; stale or wrong-job responses never claim completion. Unmounted,
other-job and signed-out callbacks cannot navigate the current workspace.

The session-only notice scrolls into view and dismisses when a new file selection starts.
Confirmed canceled IDs prevent stale recent-list responses from bringing the draft back into
the active list. Account history is not deleted or globally cleared. Fully settled upload
batches can reset on cancellation return; active, unknown and recoverable siblings are preserved.

Verification:

- TypeScript, ESLint and production build/dependency-boundary checks passed.
- All 528 web tests passed, including 14 new cancellation integration cases. The final review
  control grouping subsequently passed its 72 affected tests.
- WebKit ran all eight browser flows against the final nine-file compiled bundle. Desktop/mobile
  cancellation checks used local fixtures, with zero provider transport.
- At 360px, the review Cancel/Approve buttons share the same row, as do upload Cancel/Submit.
  The terminal cancellation notice is visible without manual scrolling; the start form is empty
  and enabled. Synthetic fixtures do not prove live backend cleanup or a real authenticated
  cancellation operation.

Final browser evidence is under `output/playwright/phase66/20261005T185517Z/` and
`output/playwright/draft-cancel-final-20261005/`, ignored by Git. The final bundle digest is
`ac9b07f8d34d4c2ebbddb569666f8d6380b85ebc36cb42c9a9fcf6292f530de0`.

The site uses one shared `index.html` and `/assets/` tree for seller and judge routes; there is
no separate `judge/index.html`. Preserve both runtime-config objects and the existing CloudFront
routing. Deployment replaces only the checked entry page and adds verified immutable assets,
retaining prior assets and the captured entry page for rollback. It does not update Lambda,
model configuration, stores, publishing, accounts or cleanup timers.

Private deployment captures/manifests are in `.mr_lister_private/draft-ui-20261005/`.
Deployment verification will be recorded after the web update completes.
