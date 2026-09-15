import { useRef, useSyncExternalStore } from "react";
import { useNavigate } from "react-router-dom";
import type { JudgeSessionCoordinator } from "./judge-session";
import { validateReturnPath } from "./session";

export function JudgeSessionEntry({ auth, returnPath = "/", preparedJobId }: {
  auth: JudgeSessionCoordinator;
  returnPath?: string;
  preparedJobId?: string;
}) {
  const state = useSyncExternalStore(auth.subscribeEntry, auth.getEntryState, auth.getEntryState);
  const navigate = useNavigate();
  const generation = useRef(0);
  const pending = state.phase === "entering" || state.phase === "restoring";
  const enter = (destination: string) => {
    const operation = ++generation.current;
    void auth.startSignIn().then(() => {
      if (operation === generation.current && auth.session.getStatus() === "authenticated") void navigate(validateReturnPath(destination), { replace: true });
    }).catch(() => { /* The coordinator supplies a fixed, safe inline message. */ });
  };
  return <div className="judge-session-entry">
    <p role={state.phase === "error" || state.phase === "signout-error" ? "alert" : "status"}>{state.message}</p>
    <div className="judge-access-actions">
      {state.phase !== "missing" && state.phase !== "signing-out" && state.phase !== "signout-error" && <button className="button button--primary" type="button" disabled={pending} onClick={() => { enter(returnPath); }}>
        {pending ? "Opening workspace…" : "Enter judge workspace"}
      </button>}
      {preparedJobId !== undefined && state.phase === "ready" && <button className="button" type="button" onClick={() => { enter(`/jobs/${encodeURIComponent(preparedJobId)}`); }}>Review prepared example</button>}
      {pending && <button className="button" type="button" onClick={() => { generation.current += 1; auth.cancel(); }}>Cancel</button>}
      {state.phase === "signout-error" && <button className="button" type="button" onClick={() => { auth.signOut(); }}>Try sign-out again</button>}
    </div>
  </div>;
}
