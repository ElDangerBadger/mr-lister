import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useAppDependencies } from "../app-context";
import { AccountSetupError, accountIdentity, accountSetupSchema, hasAccountWorkflowToken, type AccountSetup, type AccountSetupPort, type AccountWorkspaceConfiguration } from "./account-workspace";
import type { AuthSession } from "./session";
import { useSignIn } from "./sign-in";
import { StoreSetupPage } from "../store-setup/StoreSetupPage";
import { StoreConnectionError, type ConnectedStore, type StoreConnectionAdapter } from "../store-setup/connection-adapter";
import { areServiceNoticesReviewed } from "../service-notices";
import "./account-workspace.css";

/** Retain the account gate while its access token renews; logout always clears it. */
export function useAccountIdentity(session: AuthSession, config: AccountWorkspaceConfiguration | undefined): string | null {
  const previous = useRef<{ session: AuthSession; identity: string | null; known: boolean }>({ session, identity: null, known: false });
  const snapshot = useCallback(() => {
    if (config === undefined || session.getStatus() === "anonymous") {
      previous.current = { session, identity: null, known: false };
      return null;
    }
    const token = session.getAccessToken();
    if (token !== null) {
      const identity = accountIdentity(token, config);
      previous.current = { session, identity, known: true };
      return identity;
    }
    return previous.current.session === session && previous.current.known ? previous.current.identity : "renewing";
  }, [session, config]);
  const identity = useSyncExternalStore(useCallback((listener) => session.subscribe(listener), [session]), snapshot, () => null);
  useEffect(() => {
    if (identity === "renewing") void session.renewAccessToken();
  }, [identity, session]);
  return identity;
}

export function useAccountSetup(identity: string, api: AccountSetupPort | undefined, session: AuthSession) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ identity: string; value: AccountSetup } | null>(null);
  const [error, setError] = useState<AccountSetupError | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setResult(null); setError(null); setLoading(true);
    if (identity === "renewing") return () => { controller.abort(); };
    if (api === undefined) {
      setError(new AccountSetupError("unavailable")); setLoading(false);
      return () => { controller.abort(); };
    }
    void api.getSetup(identity, controller.signal).then((value) => {
      if (!controller.signal.aborted && session.getStatus() === "authenticated") setResult({ identity, value: accountSetupSchema.parse(value) });
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof AccountSetupError ? reason : new AccountSetupError("unavailable"));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => { controller.abort(); };
  }, [identity, api, session, attempt]);
  const value = result?.identity === identity ? result.value : null;
  return { value, error, loading, retry: () => { setResult(null); setLoading(true); setAttempt((current) => current + 1); }, accept: (next: AccountSetup) => { setResult({ identity, value: accountSetupSchema.parse(next) }); setError(null); setLoading(false); } };
}

export type AccountSetupController = ReturnType<typeof useAccountSetup>;

export function useAccountWorkflowToken(session: AuthSession, config: AccountWorkspaceConfiguration | undefined, identity: string): boolean {
  const previous = useRef<{ session: AuthSession; config: AccountWorkspaceConfiguration | undefined; identity: string; allowed: boolean }>({ session, config, identity, allowed: false });
  return useSyncExternalStore(useCallback((listener) => session.subscribe(listener), [session]), useCallback(() => {
    if (config === undefined || session.getStatus() === "anonymous") { previous.current = { session, config, identity, allowed: false }; return false; }
    const token = session.getAccessToken();
    if (token !== null) {
      const allowed = accountIdentity(token, config) === identity && hasAccountWorkflowToken(token, config);
      previous.current = { session, config, identity, allowed };
      return allowed;
    }
    // Preserve the same account's open workspace during renewal, never across logout.
    return previous.current.session === session && previous.current.config === config && previous.current.identity === identity && previous.current.allowed;
  }, [session, config, identity]), () => false);
}

export function AccountSetupPage({ identity, controller, onContinue }: { identity: string; controller: AccountSetupController; onContinue: (connection: ConnectedStore) => void }) {
  const { startSignIn } = useSignIn();
  const { auth, accountConfig, storeConnectionAdapter } = useAppDependencies();
  const workflowToken = useAccountWorkflowToken(auth.session, accountConfig, identity);
  const { value, error, loading } = controller;
  const personalToken = accountConfig?.connectionMethod === "personal_token" && value?.connection_method === "personal_token" && storeConnectionAdapter !== undefined;
  const workflowEnabled = accountConfig?.connectedWorkflow === true && areServiceNoticesReviewed(accountConfig.noticeVersion, accountConfig.supportEmail);
  if (!loading && error === null && personalToken && value.state !== "connection_unavailable") {
    if (value.store !== null && (value.state === "reconnect_required" || !workflowToken || !workflowEnabled)) {
      return <PendingActivation key={identity} adapter={storeConnectionAdapter} storeName={value.store.name} enabled={workflowEnabled} onConnected={controller.accept} onSignIn={() => { startSignIn("/store-setup"); }} onRefresh={controller.retry} />;
    }
    const initialConnection: ConnectedStore | undefined = value.state === "ready" && value.store !== null ? { connectionId: value.store.connection_id, store: { id: String(value.store.shop_id), name: value.store.name, salesChannel: "etsy", eligible: true, disabledReason: null }, setup: value } : undefined;
    return <StoreSetupPage key={identity} identity={identity} session={auth.session} adapter={storeConnectionAdapter} onSignIn={() => { startSignIn("/store-setup"); }} onContinue={onContinue} onRefresh={controller.retry} {...(initialConnection === undefined ? {} : { initialConnection })} />;
  }
  const unavailable = value?.state === "connection_unavailable";
  return <section className="page account-setup-page" aria-labelledby="account-setup-title">
    <div className="account-setup-intro">
      <p className="eyebrow">Your Mr. Lister workspace</p>
      <h1 id="account-setup-title">{loading ? "Getting your account ready…" : error !== null ? "Let’s finish setting up." : "Your account is ready."}</h1>
      <p>A home for your artwork, your store, and your next listing.</p>
    </div>
    <div className="account-setup-layout">
      <ol className="account-setup-steps" aria-label="Account setup progress">
        <li><span className="account-step-number" aria-hidden="true">✓</span><div><h2>Create your account</h2><p>You’re securely signed in.</p></div></li>
        <li aria-current="step"><span className="account-step-number" aria-hidden="true">2</span><div><h2>Connect Printify</h2><p>Bring your store into your workspace.</p></div></li>
        <li><span className="account-step-number" aria-hidden="true">3</span><div><h2>Create your first listing</h2><p>Start with artwork. Review before publishing.</p></div></li>
      </ol>
      <section className="account-connection-card" aria-labelledby="account-connection-title">
        <div className="account-connection-mark" aria-hidden="true"><svg width="28" height="28" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="M5 13v14h22V13M4 12l3-7h18l3 7M12 27v-9h8v9M4 12c0 5 6 5 6 0 0 5 6 5 6 0 0 5 6 5 6 0 0 5 6 5 6 0" /></svg></div>
        <p className="eyebrow">Your print partner</p>
        <h2 id="account-connection-title">Connect your Printify store.</h2>
        {loading ? <p role="status">Checking your account setup…</p> : error !== null ? <>
          <p className="alert alert--error" role="alert">{error.message}</p>
          <button className="button button--primary" type="button" onClick={() => { if (error.code === "session_expired") startSignIn("/store-setup"); else controller.retry(); }}>{error.code === "session_expired" ? "Sign in again" : "Try again"}</button>
        </> : <>
          <p>{unavailable ? "Your account is saved. Printify connections aren’t available here yet, so there’s nothing else you need to set up today." : value?.store !== null && value?.store !== undefined ? `${value.store.name} is connected. Listing creation will be available when store setup is complete.` : "Your next step is to connect Printify and choose your Etsy store. Connection setup will be available here soon."}</p>
          <div className="account-connection-status"><span aria-hidden="true" />{unavailable ? "Store connections coming soon" : "Store setup pending"}</div>
          <p className="account-setup-note">Once connected, you’ll be able to upload artwork and review your listings before anything goes live.</p>
        </>}
      </section>
    </div>
  </section>;
}

function PendingActivation({ adapter, storeName, enabled, onConnected, onSignIn, onRefresh }: { adapter: StoreConnectionAdapter; storeName: string; enabled: boolean; onConnected: (setup: AccountSetup) => void; onSignIn: () => void; onRefresh: () => void }) {
  const operation = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<"session_expired" | "unavailable" | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => () => { operation.current?.abort(); adapter.reset(); }, [adapter]);
  useEffect(() => { if (error !== null) errorRef.current?.focus(); }, [error]);
  async function activate() {
    if (operation.current !== null) return;
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setError(null);
    try {
      const connection = await adapter.activate(controller.signal);
      if (!controller.signal.aborted) onConnected(connection.setup);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof StoreConnectionError && reason.code === "session_expired" ? "session_expired" : "unavailable");
    } finally {
      if (!controller.signal.aborted) { operation.current = null; setBusy(false); }
    }
  }
  return <section className="page narrow-page account-setup-page"><p className="eyebrow">Your store is saved</p><h1>One last step.</h1><p>{storeName} is linked to your account. {enabled ? "Finish connecting to open your workspace." : "Your workspace will open when connections are available."}</p>
    {error !== null && <p ref={errorRef} className="alert alert--error" role="alert" tabIndex={-1}>{error === "session_expired" ? "Sign in again to finish opening your workspace. Your store is saved." : "We couldn’t finish connecting. Your store is saved. Please try again."}</p>}
    {error === "session_expired" ? <button className="button button--primary" type="button" onClick={onSignIn}>Sign in again</button> : <button className="button button--primary" type="button" disabled={busy} onClick={() => { if (enabled) void activate(); else onRefresh(); }}>{busy ? "Finishing connection…" : enabled ? "Finish connecting" : "Refresh setup"}</button>}
    {busy && <p role="status">Opening your connected workspace…</p>}
  </section>;
}
