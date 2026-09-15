import { useEffect, useState } from "react";
import type { ListingPricing, SellerReview } from "../contracts";

export interface PricingFormDraft {
  price: string;
  variants: Array<{ color: string; size: string; price: string }>;
  freeShipping: boolean;
}

export function pricingFormDraft(pricing: ListingPricing): PricingFormDraft {
  return {
    price: (pricing.retail_price_cents / 100).toFixed(2),
    variants: pricing.variant_prices.map((item) => ({ color: item.color, size: item.size, price: (item.retail_price_cents / 100).toFixed(2) })),
    freeShipping: pricing.free_shipping,
  };
}

export function priceCents(value: string): number | null {
  const match = /^(\d{1,4})(?:\.(\d{1,2}))?$/u.exec(value.trim());
  if (match === null) return null;
  const cents = Number(match[1]) * 100 + Number((match[2] ?? "").padEnd(2, "0"));
  return cents >= 1 && cents <= 999_999 ? cents : null;
}

export function pricingRequest(draft: PricingFormDraft): ListingPricing | null {
  const price = priceCents(draft.price);
  if (price === null || draft.variants.some((item) => priceCents(item.price) === null)) return null;
  return {
    retail_price_cents: price,
    variant_prices: draft.variants.map((item) => ({ color: item.color, size: item.size, retail_price_cents: priceCents(item.price)! })),
    free_shipping: draft.freeShipping,
  };
}

export function pricingMatches(pricing: ListingPricing | undefined, draft: PricingFormDraft): boolean {
  const request = pricingRequest(draft);
  if (pricing === undefined || request === null) return false;
  const current = pricingRequest(pricingFormDraft(pricing));
  const normalized = (value: ListingPricing | null) => value === null ? null : {
    ...value,
    variant_prices: [...value.variant_prices].sort((a, b) => a.color.localeCompare(b.color) || a.size.localeCompare(b.size)),
  };
  return JSON.stringify(normalized(current)) === JSON.stringify(normalized(request));
}

export function validatePricing(draft: PricingFormDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (priceCents(draft.price) === null) errors["pricing.price"] = "Enter an item price from $0.01 to $9,999.99, with at most two decimal places.";
  draft.variants.forEach((item, index) => {
    if (priceCents(item.price) === null) errors[`pricing.variants[${index}]`] = `Enter a price from $0.01 to $9,999.99 for ${item.color} / ${item.size}, with at most two decimal places.`;
  });
  return errors;
}

export function ListingPricingFields({ review, draft, disabled, shippingLocked = false, errors, onChange }: {
  review: SellerReview;
  draft: PricingFormDraft | undefined;
  disabled: boolean;
  shippingLocked?: boolean;
  errors: Record<string, string>;
  onChange: (draft: PricingFormDraft) => void;
}) {
  const [showVariants, setShowVariants] = useState(false);
  const hasVariantErrors = Object.keys(errors).some((path) => path.startsWith("pricing.variants"));
  useEffect(() => {
    if (hasVariantErrors) setShowVariants(true);
  }, [hasVariantErrors]);
  const pricing = review.product_policy.pricing;
  if (pricing === undefined) return null;
  const value = draft ?? pricingFormDraft(pricing);
  const variants = review.product_policy.colors.flatMap((color) => review.product_policy.sizes.map((size) => ({ color, size })));
  const changeVariant = (color: string, size: string, price: string | null) => {
    const matches = (item: { color: string; size: string }) => item.color === color && item.size === size;
    const variants = price === null ? value.variants.filter((item) => !matches(item))
      : value.variants.some(matches) ? value.variants.map((item) => matches(item) ? { ...item, price } : item)
        : [...value.variants, { color, size, price }];
    onChange({ ...value, variants });
  };

  return (
    <fieldset className="pricing-fields" disabled={disabled}>
      <legend>Pricing &amp; shipping <span>USD · This listing only</span></legend>
      <p className="pricing-intro">Set one price for the item, or give individual colors and sizes their own price.</p>
      {review.product_policy.pricing_saved === false && <p className="pricing-help">{disabled ? "This older listing shows the preset shipping choice; its shipping setting has not been verified in Printify." : "This older draft shows the preset shipping choice. Save a pricing revision to apply and verify it in Printify."}</p>}
      <div className="item-price-row">
        <div>
          <label htmlFor="listing-item-price">Item price</label>
          <div className="money-input"><span aria-hidden="true">$</span><input id="listing-item-price" type="text" inputMode="decimal" value={value.price} maxLength={12} readOnly={disabled} aria-invalid={errors["pricing.price"] !== undefined} aria-describedby={errors["pricing.price"] === undefined ? "item-price-help" : "item-price-help item-price-error"} onChange={(event) => onChange({ ...value, price: event.target.value })} /></div>
        </div>
        <button className="button" type="button" onClick={() => onChange({ ...value, variants: [] })} disabled={disabled || priceCents(value.price) === null}>Apply to all variants</button>
      </div>
      <p id="item-price-help" className="pricing-help">Used by every variant without a custom price. Applying to all replaces custom prices.</p>
      {errors["pricing.price"] !== undefined && <p id="item-price-error" className="field-error">{errors["pricing.price"]}</p>}

      <div className="shipping-setting">
        {shippingLocked ? <div>
          <strong>{value.freeShipping ? "Free shipping is selected" : "Printify standard shipping"}</strong>
          <p>{value.freeShipping
            ? "Standard shipping is required before this judge listing can be published."
            : pricing.free_shipping
              ? "Save your revision to apply standard shipping before publication."
              : "Estimates assume the buyer pays the standard US shipping cost; checkout charges can vary."}</p>
          <p className="pricing-help">Shipping is fixed for judge access.</p>
          {value.freeShipping && <button className="button" type="button" disabled={disabled} onClick={() => onChange({ ...value, freeShipping: false })}>Use standard shipping</button>}
        </div> : <>
          <div><label htmlFor="listing-free-shipping">Free shipping</label><p id="shipping-help">{value.freeShipping ? "You cover delivery. Production shipping is deducted from estimated proceeds." : "Use Printify’s standard shipping. Estimates assume the buyer pays the standard US shipping cost; checkout charges can vary."}</p></div>
          <input id="listing-free-shipping" className="shipping-switch" type="checkbox" role="switch" checked={value.freeShipping} aria-describedby="shipping-help" onChange={(event) => onChange({ ...value, freeShipping: event.target.checked })} />
        </>}
      </div>

      <details className="variant-prices" open={showVariants} onToggle={(event) => setShowVariants(event.currentTarget.open)}>
        <summary>Prices by variant <span>{value.variants.length === 0 ? `${variants.length} at item price` : `${value.variants.length} custom`}</span></summary>
        <div className="table-scroll" tabIndex={0} role="region" aria-label="Editable variant prices">
          <table>
            <caption className="visually-hidden">Retail price by color and size, in US dollars</caption>
            <thead><tr><th scope="col">Color / size</th><th scope="col">Price (USD)</th><th scope="col"><span className="visually-hidden">Reset custom price</span></th></tr></thead>
            <tbody>{variants.map(({ color, size }, index) => {
              const overrideIndex = value.variants.findIndex((item) => item.color === color && item.size === size);
              const override = value.variants[overrideIndex];
              const error = errors[`pricing.variants[${overrideIndex}]`];
              return <tr key={`${color}:${size}`}>
                <th scope="row">{color}<span className="variant-size">{size}{override !== undefined && <small>Custom</small>}</span></th>
                <td><label className="visually-hidden" htmlFor={`listing-variant-price-${index}`}>{color} / {size} price</label><div className="money-input"><span aria-hidden="true">$</span><input id={`listing-variant-price-${index}`} type="text" inputMode="decimal" maxLength={12} value={override?.price ?? value.price} readOnly={disabled} aria-invalid={error !== undefined} aria-describedby={error === undefined ? undefined : `variant-price-error-${index}`} onChange={(event) => changeVariant(color, size, event.target.value)} /></div>{error !== undefined && <small id={`variant-price-error-${index}`} className="field-error">{error}</small>}</td>
                <td>{override !== undefined && <button type="button" className="button button--quiet" aria-label={`Reset ${color} / ${size} to item price`} onClick={() => changeVariant(color, size, null)}>Reset</button>}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
      </details>
      <p className="pricing-help" role="status">{draft === undefined ? "Prices and shipping are saved with your listing revision." : "Unsaved pricing or shipping changes. Save your revision to sync with Printify and refresh the proceeds estimate."}</p>
    </fieldset>
  );
}
