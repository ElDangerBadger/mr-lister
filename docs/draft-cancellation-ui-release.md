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
Confirmed canceled entries also leave the active batch navigator and stop polling, even while
an uncanceled sibling continues. The notice confirms closure without implying that every other
draft or upload has finished.

Verification:

- TypeScript, ESLint and production build/dependency-boundary checks passed.
- All 528 web tests passed, including 14 new cancellation integration cases. The final review
  control grouping subsequently passed its 72 affected tests.
- Two subsequent batch regressions verify canceled-entry removal, continued sibling polling,
  and reset only after all remaining work is settled; all 68 affected tests passed.
- WebKit ran all eight browser flows against the final nine-file compiled bundle. Desktop/mobile
  cancellation checks used local fixtures, with zero provider transport.
- At 360px, the review Cancel/Approve buttons share the same row, as do upload Cancel/Submit.
  The terminal cancellation notice is visible without manual scrolling; the start form is empty
  and enabled. Synthetic fixtures do not prove live backend cleanup or a real authenticated
  cancellation operation.

Final browser evidence is under `output/playwright/phase66/20261005T191507Z/` and
`output/playwright/draft-cancel-final-20261005/`, ignored by Git. The final bundle digest is
`80f6b4a3ebdd7c0cabedeb08bafa3d680464fb36ee4a59cc13670e49d5cc9957`.
The screenshots precede the final batch filtering and neutral notice-body correction; geometry
is unchanged. The final exact bundle subsequently passed all eight WebKit iteration flows.

The site uses one shared `index.html` and `/assets/` tree for seller and judge routes; there is
no separate `judge/index.html`. Preserve both runtime-config objects and the existing CloudFront
routing. Deployment replaces only the checked entry page and adds verified immutable assets,
retaining prior assets and the captured entry page for rollback. It does not update Lambda,
model configuration, stores, publishing, accounts or cleanup timers.

Private deployment captures/manifests are in `.mr_lister_private/draft-ui-20261005/`.
Source was merged/pushed to `main` and deployed at
`80635667e3198d1683d50faca0d8f2aca3b668a5`. Both cache invalidations completed. Live seller,
judge and nested judge routes serve the expected shared entry page; exact public JS/CSS hashes
match the final bundle. Both runtime-config versions and CloudFront configuration are unchanged.
Readback also verified all 16 backend functions' code/environment and key settings are unchanged.
The final receipt is `verification-final.json`; no real draft or provider operation was mutated
by verification. A fresh authenticated user cancellation remains the real-service confirmation
of the existing backend endpoint, distinct from these frontend fixture and deployment checks.
