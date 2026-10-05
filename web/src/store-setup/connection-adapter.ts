import type { AccountSetup } from "../auth/account-workspace";
import type { MerchantAuthorization } from "../service-notices";

/** Only the adapter may cross a connection API boundary. It owns response validation.
 * Identity comes from the authenticated server session, never from form fields.
 * Validation handles and connection IDs are opaque, session-bound references, not credentials.
 */
export interface StoreChoice {
  readonly id: string;
  readonly name: string;
  readonly salesChannel: string;
  readonly eligible: boolean;
  readonly disabledReason: "unsupported_channel" | "disconnected" | null;
}

export interface ValidatedConnection {
  readonly validationId: string;
}

export interface ConnectedStore {
  readonly connectionId: string;
  readonly store: StoreChoice;
  readonly setup: AccountSetup;
}

export type ConnectionErrorCode =
  | "authorization_required"
  | "invalid_credentials"
  | "missing_permissions"
  | "connection_failed"
  | "session_expired"
  | "validation_expired"
  | "setup_unavailable";

export class StoreConnectionError extends Error {
  constructor(readonly code: ConnectionErrorCode) {
    super("Store connection could not be completed.");
    this.name = "StoreConnectionError";
  }
}

export interface StoreConnectionAdapter {
  readonly mode: "simulated" | "live";
  validate(token: string, authorization: MerchantAuthorization, signal: AbortSignal): Promise<ValidatedConnection>;
  listStores(validationId: string, signal: AbortSignal): Promise<readonly StoreChoice[]>;
  connect(validationId: string, storeId: string, signal: AbortSignal): Promise<ConnectedStore>;
  activate(signal: AbortSignal): Promise<ConnectedStore>;
  reset(): void;
}
