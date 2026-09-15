# Temporary judge pricing and shipping policy

This is implementation documentation, not a deployment receipt.

New judge listings start at **$35.00 USD** for every configured variant, with standard
paid shipping. Prices remain editable. The owner account retains its existing defaults
and shipping controls. This policy does not guarantee a profit or determine the final
Etsy checkout shipping charge.

The optional `MR_LISTER_JUDGE_PRICING_POLICY` environment setting contains a strict JSON
object with `owner_id`, `default_price_cents` and optional `minimum_price_cents`. The
release uses the exact dedicated judge owner, `3500` cents and no minimum. Missing
configuration disables the policy; malformed configuration fails startup. There is no
configurable free-shipping exception: the active policy requires it to be false.

The preparation worker applies defaults only when creating the first review, before
computing its immutable fingerprint. Resumed checkpoints and saved reviews retain
their recorded prices. The fixed product profile and its fingerprints are unchanged.
The review query displays the judge defaults while that first review is being built.

The seller command service rejects judge revisions and approvals with free shipping
or missing explicit pricing. The publication request service also rejects an older,
already approved judge review with those settings. These checks use the authenticated
owner, so opening the ordinary application URL cannot bypass them. A configured
minimum, when present, applies to both the item price and every variant override.

The judge editor displays the saved shipping choice without a free-shipping switch.
An editable older draft with free shipping offers **Use standard shipping**; the user
must then save normally. That creates a review revision, invalidates approval and
economics, and synchronizes the same Printify draft. A previously approved listing
that can no longer be edited must be replaced with a new listing. Existing products,
approved evidence and live Etsy listings are never silently rewritten.

Release the matching AgentCore preparation runtime, seller command/query code and
publication request code with the same policy, followed by the compatible web bundle.
Verify that no earlier judge publication is queued before activating the restriction.
Retain the existing application release authority and exact product/store bindings.
Removing the server configuration and restoring the judge shipping control after
judging are deliberate release actions; removing the policy does not reset saved prices.

The accompanying review-opening fix keeps the neutral activity indicator visible
during bounded initial read retries. It still reports nonretryable errors and retry
exhaustion. The September 14 user upload completed normally in server logs; the exact
cause of its brief browser error was not established by those logs.
