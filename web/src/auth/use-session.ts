import { useSyncExternalStore } from "react";
import type { AuthSession, SessionStatus } from "./session";

export function useSessionStatus(session: AuthSession): SessionStatus {
  return useSyncExternalStore(
    (listener) => session.subscribe(listener),
    () => session.getStatus(),
    () => "anonymous",
  );
}
