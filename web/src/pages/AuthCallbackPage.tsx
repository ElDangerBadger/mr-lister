import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";

export function AuthCallbackPage() {
  const { auth } = useAppDependencies();
  const location = useLocation();
  const navigate = useNavigate();
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void auth.completeSignIn(location.search).then((returnPath) => {
      void navigate(returnPath, { replace: true });
    }).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "Sign-in could not be completed.");
    });
  }, [auth, location.search, navigate]);

  return (
    <section className="page narrow-page" aria-labelledby="callback-heading">
      <p className="eyebrow">Secure sign-in</p>
      <h1 id="callback-heading">{error === null ? "Verifying your session…" : "Sign-in needs another try"}</h1>
      {error === null ? <div className="loading-line" role="status">Exchanging the one-use authorization code.</div> : (
        <div className="alert alert--error" role="alert">
          <p>{error}</p>
          <button className="button" type="button" onClick={() => { void auth.startSignIn("/"); }}>Try sign-in again</button>
        </div>
      )}
    </section>
  );
}
