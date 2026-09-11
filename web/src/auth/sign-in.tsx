import { createContext, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { AuthError } from "./session";

interface SignInFlow {
  startSignIn: (returnPath: string) => void;
  error: string | null;
}

type SignInState = {
  phase: "popup" | "redirect" | "error";
  returnPath: string;
  message: string | null;
};

const SignInContext = createContext<SignInFlow | null>(null);

export function SignInProvider({ children }: { children: ReactNode }) {
  const { auth } = useAppDependencies();
  const navigate = useNavigate();
  const [state, setState] = useState<SignInState | null>(null);
  const generation = useRef(0);
  const mounted = useRef(true);
  const ownsPopup = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const restoreFocus = useRef(false);
  const titleId = useId();
  const descriptionId = useId();
  const open = state !== null;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
      if (ownsPopup.current) {
        ownsPopup.current = false;
        auth.cancelPopupSignIn?.();
      }
    };
  }, [auth]);

  useEffect(() => {
    const element = dialog.current;
    if (!open || element === null) {
      if (restoreFocus.current) {
        restoreFocus.current = false;
        if (trigger.current?.isConnected) trigger.current.focus();
      }
      return;
    }
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    heading.current?.focus();
    return () => {
      if (element.open && typeof element.close === "function") element.close();
    };
  }, [open]);

  useEffect(() => {
    if (state?.phase === "error") heading.current?.focus();
  }, [state?.phase]);

  const fail = (operation: number, returnPath: string, reason: unknown) => {
    if (!mounted.current || generation.current !== operation) return;
    ownsPopup.current = false;
    setState({ phase: "error", returnPath, message: safeSignInError(reason) });
  };

  const begin = (returnPath: string, popup: boolean, rememberTrigger: boolean) => {
    if (rememberTrigger) trigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const operation = ++generation.current;
    const usePopup = popup && auth.startPopupSignIn !== undefined;
    ownsPopup.current = usePopup;
    setState({ phase: usePopup ? "popup" : "redirect", returnPath, message: null });
    // Open during the click event, before any asynchronous work can consume the browser's user activation.
    try {
      if (usePopup && auth.startPopupSignIn !== undefined) {
        void auth.startPopupSignIn(returnPath).then(async (destination) => {
          if (!mounted.current || generation.current !== operation) return;
          ownsPopup.current = false;
          setState(null);
          await navigate(destination, { replace: true });
        }).catch((reason: unknown) => { fail(operation, returnPath, reason); });
      } else {
        auth.cancelPopupSignIn?.();
        void auth.startSignIn(returnPath).then(() => {
          if (mounted.current && generation.current === operation) setState(null);
        }).catch((reason: unknown) => { fail(operation, returnPath, reason); });
      }
    } catch (reason: unknown) {
      fail(operation, returnPath, reason);
    }
  };

  const dismiss = () => {
    generation.current += 1;
    ownsPopup.current = false;
    auth.cancelPopupSignIn?.();
    restoreFocus.current = true;
    setState(null);
  };

  const focusPopup = () => {
    try {
      auth.focusPopupSignIn?.();
    } catch (reason: unknown) {
      if (state !== null) fail(generation.current, state.returnPath, reason);
    }
  };

  return (
    <SignInContext.Provider value={{ startSignIn: (returnPath) => { begin(returnPath, true, true); }, error: null }}>
      {children}
      {state !== null && (
        <dialog
          ref={dialog}
          className="confirmation-dialog"
          aria-labelledby={titleId}
          aria-describedby={descriptionId}
          onCancel={(event) => { event.preventDefault(); dismiss(); }}
        >
          <p className="eyebrow">Your workspace</p>
          <h3 ref={heading} id={titleId} tabIndex={-1}>{state.phase === "error" ? "Sign-in needs another try" : "Sign in to Mr. Lister"}</h3>
          <p id={descriptionId} role={state.phase === "error" ? "alert" : "status"}>
            {state.phase === "error"
              ? state.message
              : state.phase === "popup"
                ? "Complete secure sign-in in the small window. Your workspace will be ready here when you finish."
                : "Opening secure sign-in…"}
          </p>
          <div className="form-actions">
            {state.phase === "popup" && <button className="button button--primary" type="button" onClick={focusPopup}>Show sign-in window</button>}
            {state.phase === "error" && <>
              {auth.startPopupSignIn !== undefined && <button className="button button--primary" type="button" onClick={() => { begin(state.returnPath, true, false); }}>Try popup again</button>}
              <button className="button" type="button" onClick={() => { begin(state.returnPath, false, false); }}>Continue in this tab</button>
            </>}
            <button className="button" type="button" onClick={dismiss}>Cancel</button>
          </div>
        </dialog>
      )}
    </SignInContext.Provider>
  );
}

export function useSignIn(): SignInFlow {
  const context = useContext(SignInContext);
  const { auth } = useAppDependencies();
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  if (context !== null) return context;
  // Standalone page fixtures can keep using their existing full-page auth coordinator.
  return {
    error,
    startSignIn: (returnPath) => {
      setError(null);
      try {
        void auth.startSignIn(returnPath).catch((reason: unknown) => {
          if (mounted.current) setError(safeSignInError(reason));
        });
      } catch (reason: unknown) {
        if (mounted.current) setError(safeSignInError(reason));
      }
    },
  };
}

function safeSignInError(reason: unknown): string {
  return reason instanceof AuthError ? reason.message : "Sign-in could not be completed. Please try again.";
}
