import { z } from "zod";

/** Bump only with a reviewed change to the notices shown before authorization. */
export const SERVICE_NOTICE_VERSION = "2026-10-05" as const;

const supportEmailSchema = z.string().email().max(254);
export function serviceSupportEmail(value: string | undefined): string | null {
  const result = supportEmailSchema.safeParse(value);
  return result.success ? result.data : null;
}

export function areServiceNoticesReviewed(noticeVersion: string | undefined, supportEmail: string | undefined): boolean {
  return noticeVersion === SERVICE_NOTICE_VERSION && serviceSupportEmail(supportEmail) !== null;
}

export interface MerchantAuthorization {
  readonly accepted: true;
  readonly terms_version: typeof SERVICE_NOTICE_VERSION;
  readonly privacy_version: typeof SERVICE_NOTICE_VERSION;
}
