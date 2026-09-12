import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode, type RefAttributes } from "react";
import { Link, useLinkClickHandler, useLocation, type LinkProps, type To } from "react-router-dom";

export type NavigationProtectionReason = "unsaved" | "saving" | "reconciling" | "none";

type Protection = { scope: string; reason: NavigationProtectionReason };
type PendingNavigation = { scope: string; proceed: () => void; trigger: HTMLElement | null };
interface WorkspaceNavigation {
  protect: (owner: symbol, protection: Protection) => () => void;
  requestNavigation: (proceed: () => void, trigger?: HTMLElement) => void;
}

const NavigationContext = createContext<WorkspaceNavigation | null>(null);

/** Protects workspace links without replacing the application's existing router. */
export function WorkspaceNavigationProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const scope = `${location.key}:${location.pathname}${location.search}${location.hash}`;
  const [protections, setProtections] = useState(new Map<symbol, Protection>());
  const [pending, setPending] = useState<PendingNavigation | null>(null);
  const restoreFocus = useRef<HTMLElement | null>(null);
  const skipUnloadWarning = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const stayButton = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const reason = strongestProtection(protections, scope);
  const open = pending !== null && pending.scope === scope && reason !== "none";

  const protect = useCallback((owner: symbol, protection: Protection) => {
    setProtections((current) => new Map(current).set(owner, protection));
    return () => {
      setProtections((current) => {
        if (current.get(owner) !== protection) return current;
        const next = new Map(current);
        next.delete(owner);
        return next;
      });
      // A pending destination belongs to this review state, never a later job.
      setPending((current) => current?.scope === protection.scope ? null : current);
    };
  }, []);

  const requestNavigation = useCallback((proceed: () => void, trigger?: HTMLElement) => {
    if (reason === "none") {
      proceed();
      return;
    }
    setPending({ scope, proceed, trigger: trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null) });
  }, [reason, scope]);

  useEffect(() => {
    skipUnloadWarning.current = false;
    if (reason === "none") return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (skipUnloadWarning.current) {
        skipUnloadWarning.current = false;
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => { window.removeEventListener("beforeunload", warnBeforeUnload); };
  }, [reason, scope]);

  useEffect(() => {
    const element = dialog.current;
    if (!open || element === null) {
      if (restoreFocus.current?.isConnected) restoreFocus.current.focus();
      restoreFocus.current = null;
      return;
    }
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    stayButton.current?.focus();
    return () => {
      if (element.open && typeof element.close === "function") element.close();
    };
  }, [open]);

  const dismiss = () => {
    restoreFocus.current = pending?.trigger ?? null;
    setPending(null);
  };
  const leave = () => {
    if (pending === null || pending.scope !== scope || reason !== "unsaved") return;
    const proceed = pending.proceed;
    setPending(null);
    skipUnloadWarning.current = true;
    proceed();
  };
  const handleDialogKeys = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      dismiss();
    } else if (event.key === "Tab") {
      // Native dialogs trap focus; this also covers browsers using the fallback.
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
      const first = buttons[0];
      const last = buttons.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
  };
  const context = useMemo(() => ({ protect, requestNavigation }), [protect, requestNavigation]);

  return (
    <NavigationContext.Provider value={context}>
      {children}
      {open && (
        <dialog ref={dialog} className="confirmation-dialog" aria-labelledby={titleId} aria-describedby={descriptionId}
          onCancel={(event) => { event.preventDefault(); dismiss(); }} onKeyDown={handleDialogKeys}>
          <p className="eyebrow">Your listing</p>
          <h3 id={titleId}>{reason === "unsaved" ? "Leave your unsaved changes?" : reason === "saving" ? "Your changes are saving" : "Your listing is updating"}</h3>
          <p id={descriptionId}>{reason === "unsaved"
            ? "You have edits that haven’t been saved. Stay here to finish, or leave without saving these changes."
            : reason === "saving"
              ? "Please wait while your listing changes are saved. You can move to another page once saving finishes."
              : "Your saved changes are being checked against the updated product. Please wait for this to finish before leaving."}</p>
          <div className="form-actions">
            <button ref={stayButton} type="button" className="button button--primary" onClick={dismiss}>Stay here</button>
            {reason === "unsaved" && <button type="button" className="button button--danger" onClick={leave}>Leave without saving</button>}
          </div>
        </dialog>
      )}
    </NavigationContext.Provider>
  );
}

/** Register the current review's edit barrier; standalone page fixtures remain supported. */
export function useNavigationProtection(reason: NavigationProtectionReason) {
  const context = useContext(NavigationContext);
  const protect = context?.protect;
  const owner = useRef(Symbol("workspace-review"));
  const location = useLocation();
  const scope = `${location.key}:${location.pathname}${location.search}${location.hash}`;
  useLayoutEffect(() => {
    if (reason === "none" || protect === undefined) return;
    return protect(owner.current, { scope, reason });
  }, [protect, reason, scope]);
}

/** A normal router link, with confirmation only when replacing this tab's review. */
export function WorkspaceLink(props: LinkProps & RefAttributes<HTMLAnchorElement>) {
  const context = useContext(NavigationContext);
  const { target, ...routerOptions } = props;
  const routerClick = useLinkClickHandler<HTMLAnchorElement>(routerDestination(props.to), { ...routerOptions, ...(target === undefined ? {} : { target }) });
  const location = useLocation();
  return <Link {...props} reloadDocument={props.reloadDocument || (props.download !== undefined && props.download !== false)} onClick={(event) => {
    props.onClick?.(event);
    if (event.defaultPrevented || context === null || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || (props.target !== undefined && props.target !== "_self") || event.currentTarget.hasAttribute("download")) return;
    const trigger = event.currentTarget;
    const destination = new URL(trigger.href, window.location.href);
    const sameOrigin = destination.origin === window.location.origin;
    if (!props.reloadDocument && sameOrigin && `${destination.pathname}${destination.search}${destination.hash}` === `${location.pathname}${location.search}${location.hash}`) return;
    event.preventDefault();
    context.requestNavigation(() => {
      if (props.reloadDocument || !sameOrigin) {
        if (props.replace) window.location.replace(destination.href);
        else window.location.assign(destination.href);
      } else routerClick(event);
    }, trigger);
  }} />;
}

function routerDestination(to: To): To {
  if (typeof to !== "string" || !/^(?:[a-z][a-z0-9+.-]*:|\/\/)/iu.test(to)) return to;
  const destination = new URL(to, window.location.href);
  return destination.origin === window.location.origin ? { pathname: destination.pathname, search: destination.search, hash: destination.hash } : to;
}

function strongestProtection(protections: Map<symbol, Protection>, scope: string): NavigationProtectionReason {
  const reasons = [...protections.values()].filter((protection) => protection.scope === scope).map((protection) => protection.reason);
  if (reasons.includes("saving")) return "saving";
  if (reasons.includes("reconciling")) return "reconciling";
  return reasons.includes("unsaved") ? "unsaved" : "none";
}
