# Landing page fonts

The approved Sites landing page uses Lora and DM Sans. These Latin, normal-style,
variable WOFF2 files are self-hosted so the production page retains its existing
same-origin font policy.

Source packages: `@fontsource-variable/lora@5.3.0` and
`@fontsource-variable/dm-sans@5.3.0`, downloaded from the npm registry with the
published SHA-512 archive integrity checked before extracting the two files.
No package scripts were run, and no runtime dependency was added.

Both fonts use SIL Open Font License 1.1. Original license notices are retained
beside the fonts and embedded as legal comments in `../../landing-fonts.css`, so
they are included with the deployed CSS. Preserve those notices when updating.

The release manifest validates the exact font bytes. A font update must also
update its reviewed fingerprint in `tools/prepare_phase718_web_release.py`.
