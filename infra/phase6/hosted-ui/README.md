# Seller sign-in branding

`branding.css` and `logo.jpg` are the cosmetic branding applied to the existing
Cognito Hosted UI (classic) seller app client on September 11, 2026. They are not
part of the React bundle. The original PNG in `web/src/assets` remains unchanged;
the JPEG is a 360 × 360 encoding of that artwork for Cognito's upload size limit.

The branding uses the application's green buttons, dark green labels and error
colors, with 44px inputs and the existing Mr. Lister icon in the banner. Input
boundaries use a darker neutral for at least 3:1 contrast against white; the provider's
focus glow and green focus border remain.
Only Cognito's supported customization selectors are used. It does not alter
identity providers, user-pool tier, domain branding version, OAuth settings, MFA,
password policy, account access, or the app's popup flow.

Classic branding has material limits: the provider's gray `body` background,
square outer card, and base font are outside its supported selector set. The
app's Light/Dark/Auto preference cannot be propagated through `@media`, which
Cognito rejects. This is a closer visual match within classic branding, not a
replacement of the hosted login layout. Both the email/password screen and the
MFA code screen have been observed live with this branding.

Applied source: `dfc0bb6cdbdf52702dca99f49294d129807a9412`. Cognito CSS version:
`20260912000809`. Exact CSS and public logo bytes were verified at
`2026-09-12T00:08:40.033390+00:00`, with authentication and default branding
unchanged. Private before/apply/readback evidence is under
`.mr_lister_private/cognito-branding-20260911/`; release details are recorded in
`docs/ui-and-evaluator-release.md`.

## Apply and verify

1. Confirm the current branch and the exact existing pool/domain/app client.
2. Capture `get-ui-customization` for both the client and `ALL`, plus pool/client
   and domain settings. Preserve any existing logo bytes and CSS for rollback.
3. Verify the client is still the deployed public seller app client and classic
   branding is active. Compare the candidate CSS and logo to this review.
4. Call `set-ui-customization` with the exact app client, `--css file://.../branding.css`
   and `--image-file fileb://.../logo.jpg` together. Do not apply the override to
   `ALL` or modify the domain/user-pool configuration.
5. Read back CSS and the served logo, checking their bytes against the candidate.
   Confirm pool, client, domain, and MFA settings are unchanged.
6. Allow the provider's cache to update, then open a new popup from the live app.
   Check login, errors, MFA, cancellation, and successful return to the workspace.
   The seller completes credentials/MFA. Do not store passwords, codes, or tokens.
7. Record the returned CSS version and visual verification status in
   `docs/ui-and-evaluator-release.md`. Roll back using the captured previous
   client branding if needed; do not change authentication settings.

AWS limits: logo ≤100 KB, recommended CSS ≤3 KB, combined request ≤135 KB after
image base64 encoding. The files are 45,001 image bytes and 1,928 CSS bytes.

Sources:

- [Classic branding](https://docs.aws.amazon.com/cognito/latest/developerguide/hosted-ui-classic-branding.html)
- [SetUICustomization and CSS template](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_SetUICustomization.html)

Temporary provider markup and a non-submitting style preview remain outside the
production source. Browser URL policy rejected that file preview; it was not used
as acceptance evidence. The subsequently deployed HTTPS login and MFA screens
provide the visual verification recorded above.
