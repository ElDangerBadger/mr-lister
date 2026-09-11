import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { AuthError, relayPopupCallback } from "../auth/session";

export function AuthCallbackPage() {
  const { auth } = useAppDependencies();
  const location = useLocation();
  const navigate = useNavigate();
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    try {
      if (relayPopupCallback(location.search, (reason) => {
        if (mounted.current) setError(signInErrorMessage(reason));
      })) return;
    } catch (reason: unknown) {
      setError(signInErrorMessage(reason));
      return;
    }
    void auth.completeSignIn(location.search).then(async (returnPath) => {
      if (!mounted.current) return;
      await navigate(returnPath, { replace: true });
    }).catch((reason: unknown) => {
      if (mounted.current) setError(signInErrorMessage(reason));
    });
  }, [auth, location.search, navigate]);

  return (
    <section className="page narrow-page" aria-labelledby="callback-heading">
      <p className="eyebrow">Secure sign-in</p>
      <h1 id="callback-heading">{error === null ? "Verifying your session…" : "Sign-in needs another try"}</h1>
      {error === null ? <div className="loading-line" role="status">Exchanging the one-use authorization code.</div> : (
        <div className="alert alert--error" role="alert">
          <p>{error}</p>
          <button className="button" type="button" onClick={() => {
            try {
              void auth.startSignIn("/").catch((reason: unknown) => {
                if (mounted.current) setError(signInErrorMessage(reason));
              });
            } catch (reason: unknown) {
              setError(signInErrorMessage(reason));
            }
          }}>Try sign-in again</button>
        </div>
      )}
    </section>
  );
}

function signInErrorMessage(reason: unknown): string {
  return reason instanceof AuthError ? reason.message : "Sign-in could not be completed. Please try again.";
}
