import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { AuthSession } from "../auth/session";
import { useSessionStatus } from "../auth/use-session";
import { StoreConnectionError, type ConnectedStore, type ConnectionErrorCode, type StoreChoice, type StoreConnectionAdapter } from "./connection-adapter";
import { ServiceNoticeLinks } from "../pages/ServiceNotices";
import { SERVICE_NOTICE_VERSION } from "../service-notices";
import "./store-setup.css";

interface Props {
  identity: string;
  session: AuthSession;
  adapter: StoreConnectionAdapter;
  onSignIn: () => void;
  onContinue: (store: ConnectedStore) => void;
  onRefresh: () => void;
  initialConnection?: ConnectedStore;
  sampleToken?: string;
}

type SetupState =
  | { step: "token"; error?: ConnectionErrorCode | "empty_token" }
  | { step: "validating" }
  | { step: "loading"; validationId: string }
  | { step: "load_error"; validationId: string }
  | { step: "empty"; validationId: string }
  | { step: "choose"; validationId: string; stores: readonly StoreChoice[]; selected: string; error?: "selection_required" | "connection_failed" }
  | { step: "connecting"; validationId: string; stores: readonly StoreChoice[]; selected: string }
  | { step: "connected"; connection: ConnectedStore };

const errors: Record<ConnectionErrorCode | "empty_token", { title: string; detail: string }> = {
  empty_token: { title: "Add your connection token.", detail: "Enter a Printify personal access token to find your stores." },
  authorization_required: { title: "Authorize the connection to continue.", detail: "Review the privacy notice and terms, then confirm you authorize Mr. Lister to receive and use this token." },
  invalid_credentials: { title: "This token couldn’t read your stores.", detail: "Check that the token is current and includes shops.read access in Printify, then try again." },
  missing_permissions: { title: "This token needs more permissions.", detail: "Check the listing permissions in the instructions below, then enter a replacement token." },
  connection_failed: { title: "We couldn’t confirm the connection.", detail: "Check your connection and try again. If you already chose a store, refreshing setup will check whether it was saved." },
  session_expired: { title: "Your session needs to be renewed.", detail: "Sign in again before continuing. The connection token has been cleared." },
  validation_expired: { title: "Let’s check your connection again.", detail: "The store selection has expired. Enter your token again to refresh it." },
  setup_unavailable: { title: "Let’s check your saved connection.", detail: "Your store may be saved, but the workspace isn’t ready yet. Refresh setup to see the next step." },
};

export function StoreSetupPage(props: Props) {
  const status = useSessionStatus(props.session);
  const simulated = props.adapter.mode === "simulated";
  return <section className="page store-setup-page" aria-labelledby="store-setup-heading">
    <div className="store-setup-title">
      <p className="eyebrow">Your workspace starts here</p>
      <h1 id="store-setup-heading">Set up your store.</h1>
      <p>Bring your artwork and your storefront together.</p>
    </div>
    {status !== "authenticated" ? <div className="store-setup-signin panel">
      <div className="store-setup-symbol"><StoreIcon /></div>
      <h2>First, make this workspace yours.</h2>
      <p>Sign in before connecting Printify. Your store and listings belong to your account.</p>
      {simulated && <p className="store-setup-muted">This local preview uses a sample account. No real sign-in is required.</p>}
      <button className="button button--primary" type="button" onClick={props.onSignIn}>Sign in to continue</button>
    </div> : <AuthenticatedSetup {...props} />}
  </section>;
}

function AuthenticatedSetup({ identity, session, adapter, onSignIn, onContinue, onRefresh, initialConnection, sampleToken }: Props) {
  const initialState = useRef<SetupState>(initialConnection === undefined ? { step: "token" } : { step: "connected", connection: initialConnection });
  const [state, setState] = useState<SetupState>(initialState.current);
  const [authorized, setAuthorized] = useState(false);
  const tokenInput = useRef<HTMLInputElement>(null);
  const registerTokenInput = useCallback((node: HTMLInputElement | null) => {
    // Erase the DOM value before a form transition or unmount detaches the field.
    if (tokenInput.current && tokenInput.current !== node) tokenInput.current.value = "";
    tokenInput.current = node;
  }, []);
  const focusTarget = useRef<HTMLHeadingElement>(null);
  const operation = useRef<AbortController | null>(null);
  const simulated = adapter.mode === "simulated";
  const busy = state.step === "validating" || state.step === "loading" || state.step === "connecting";
  const step = state.step === "connected" ? 3
    : ["loading", "load_error", "empty", "choose", "connecting"].includes(state.step) ? 2 : 1;

  function clearInput() { if (tokenInput.current) tokenInput.current.value = ""; }
  function cancelOperation() { operation.current?.abort(); operation.current = null; clearInput(); }
  function reset() { cancelOperation(); adapter.reset(); setAuthorized(false); setState({ step: "token" }); }

  useEffect(() => {
    // The parent keys this form by account identity; renewal of that identity is safe.
    const discard = () => {
      if (session.getStatus() === "authenticated") return;
      operation.current?.abort(); operation.current = null;
      if (tokenInput.current) tokenInput.current.value = "";
      adapter.reset();
      setAuthorized(false);
      setState({ step: "token" });
    };
    const unsubscribe = session.subscribe(discard);
    return () => {
      unsubscribe(); operation.current?.abort(); operation.current = null;
      adapter.reset();
      if (tokenInput.current) tokenInput.current.value = "";
    };
  }, [identity, session, adapter]);

  const error = state.step === "token" ? state.error : state.step === "choose" ? state.error : undefined;
  useEffect(() => {
    if (state.step === "validating" || state.step === "loading" || state.step === "connecting") return;
    if (error !== undefined) document.getElementById("store-setup-error")?.focus();
    else focusTarget.current?.focus();
  }, [state.step, error]);

  function begin(): AbortController | null {
    if (operation.current !== null) return null;
    if (session.getStatus() !== "authenticated") { clearInput(); return null; }
    const controller = new AbortController(); operation.current = controller;
    return controller;
  }
  function current(controller: AbortController): boolean {
    return operation.current === controller && !controller.signal.aborted && session.getStatus() === "authenticated";
  }
  function fail(error: unknown, controller: AbortController, fallback: SetupState) {
    if (!current(controller)) return;
    operation.current = null;
    const code = error instanceof StoreConnectionError ? error.code : "connection_failed";
    // Provider messages can include secrets. Only known local copy is displayed.
    setState(code === "connection_failed" ? fallback : { step: "token", error: code });
  }
  async function loadStores(validationId: string, controller: AbortController) {
    try {
      const stores = await adapter.listStores(validationId, controller.signal);
      if (!current(controller)) return;
      operation.current = null;
      setState(stores.length === 0 ? { step: "empty", validationId }
        : { step: "choose", validationId, stores, selected: "" });
    } catch (error) { fail(error, controller, { step: "load_error", validationId }); }
  }
  async function validate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (operation.current !== null) return;
    if (!authorized) { setState({ step: "token", error: "authorization_required" }); return; }
    let token = tokenInput.current?.value.trim() ?? "";
    clearInput();
    if (!token) { setState({ step: "token", error: "empty_token" }); return; }
    const controller = begin();
    if (controller === null) { token = ""; return; }
    setState({ step: "validating" });
    try {
      const result = adapter.validate(token, { accepted: true, terms_version: SERVICE_NOTICE_VERSION, privacy_version: SERVICE_NOTICE_VERSION }, controller.signal);
      token = "";
      const validated = await result;
      if (!current(controller)) return;
      setState({ step: "loading", validationId: validated.validationId });
      await loadStores(validated.validationId, controller);
    } catch (error) {
      token = "";
      fail(error, controller, { step: "token", error: "connection_failed" });
    }
  }
  async function retryStores(validationId: string) {
    const controller = begin(); if (controller === null) return;
    setState({ step: "loading", validationId });
    await loadStores(validationId, controller);
  }
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state.step !== "choose") return;
    if (!state.stores.some((store) => store.id === state.selected && store.eligible && store.salesChannel === "etsy")) { setState({ ...state, error: "selection_required" }); return; }
    const controller = begin(); if (controller === null) return;
    const selection = state;
    setState({ step: "connecting", validationId: selection.validationId, stores: selection.stores, selected: selection.selected });
    try {
      const connection = await adapter.connect(selection.validationId, selection.selected, controller.signal);
      if (!current(controller)) return;
      if (connection.store.id !== selection.selected) throw new StoreConnectionError("connection_failed");
      operation.current = null;
      setState({ step: "connected", connection });
    } catch (error) { fail(error, controller, { ...selection, error: "connection_failed" }); }
  }

  return <div className="store-setup-grid">
    <aside className="store-setup-guide" aria-label="Store setup steps">
      <div className="store-setup-guide-art" aria-hidden="true"><StoreIcon /><span className="store-setup-guide-spark">✦</span></div>
      <h2>Your store.<br />Your next chapter.</h2>
      <p>Connect once, then move from finished artwork to a listing you’re proud to publish.</p>
      <ol className="store-setup-steps">
        {[
          ["Connect Printify", "Start with a secure connection token."],
          ["Choose your store", "Pick where your listings will go."],
          ["Make it yours", "You review. You approve. You publish."],
        ].map(([title, detail], index) => <li key={title} aria-current={step === index + 1 ? "step" : undefined} className={step > index + 1 ? "is-complete" : ""}>
          <span className="store-setup-step-number" aria-hidden="true">{step > index + 1 ? "✓" : `0${String(index + 1)}`}</span>
          <div><strong>{title}</strong><p>{detail}</p></div>
        </li>)}
      </ol>
      <div className="store-setup-promise"><ShieldIcon /><p>Connecting a store won’t publish a listing. You stay in control of what goes live.</p></div>
    </aside>
    <section className="store-setup-card" aria-label="Printify connection">
      <div className="store-setup-card-top"><span className="store-setup-provider"><StoreIcon />Printify</span><span className="store-setup-stage">Step {step} of 3</span></div>
      <div className="store-setup-card-body">
        {state.step === "token" && <>
          <p className="eyebrow">Connect your print partner</p>
          <h2 ref={focusTarget} tabIndex={-1}>Let’s find your store.</h2>
          <p className="store-setup-description">Connect Printify so Mr. Lister can prepare listings and publish them after your approval. You’ll choose the store next.</p>
          {state.error !== undefined && <ErrorNotice title={errors[state.error].title} detail={errors[state.error].detail} />}
          {state.error === "session_expired" && <button className="button" type="button" onClick={onSignIn}>Sign in to continue</button>}
          {state.error === "setup_unavailable" ? <button className="button button--primary" type="button" onClick={onRefresh}>Refresh setup</button> : <form onSubmit={(event) => { void validate(event); }} noValidate>
            <label className="store-setup-label" htmlFor="printify-connection-key">Connection token</label>
            <p className="store-setup-input-help" id="connection-key-help">Use a Printify personal access token created for Mr. Lister.</p>
            <input ref={registerTokenInput} id="printify-connection-key" className="store-setup-input" type="password" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} maxLength={4096}
              aria-describedby={`connection-key-help connection-key-privacy${state.error ? " store-setup-error" : ""}`}
              aria-invalid={state.error === "empty_token" || state.error === "invalid_credentials"}
              placeholder={simulated ? "Use the sample token below" : "Paste your connection token"} />
            <p className="store-setup-input-help" id="connection-key-privacy">{simulated ? "Use sample data only. Don’t paste a real Printify token into this preview." : "Your token is sent securely to Mr. Lister and stored encrypted on AWS for this connection. It clears from this form when submitted."}</p>
            <div className="store-setup-authorization">
              <label><input type="checkbox" checked={authorized} onChange={(event) => { setAuthorized(event.target.checked); }} aria-describedby="connection-authorization-notices" /><span>I’m authorized to connect this Printify account, and I authorize Mr. Lister to securely receive, store, and use this token to prepare listings for my selected store and publish only after my approval.</span></label>
              <p id="connection-authorization-notices">Review <ServiceNoticeLinks newTab /> before connecting. Both open in a new tab.</p>
            </div>
            <div className="store-setup-actions">
              <button className="button button--primary" type="submit" disabled={!authorized}>Find my stores <ArrowIcon /></button>
              {simulated && sampleToken !== undefined && <button className="button button--quiet" type="button" onClick={() => { if (tokenInput.current) { tokenInput.current.value = sampleToken; tokenInput.current.focus(); } }}>Use sample token</button>}
            </div>
          </form>}
          <details className="store-setup-instructions">
            <summary>Where do I get my connection token?</summary>
            <p>{simulated ? "These steps describe the future live setup. For this preview, choose “Use sample token” above." : "Create a dedicated token for Mr. Lister so you can manage its access separately."}</p>
            <ol>
              <li>In Printify, open <strong>My profile → Connections</strong>.</li>
              <li>Create a personal access token named <strong>Mr. Lister</strong>.</li>
              <li>Enable <code>shops.read</code>, <code>catalog.read</code>, <code>products.read</code>, <code>products.write</code>, <code>uploads.read</code>, <code>uploads.write</code>, and <code>print_providers.read</code>. Leave order and webhook permissions off.</li>
              <li>Copy the token when Printify shows it, then paste it into this form.</li>
            </ol>
            <p>Finding stores checks store-reading access. Product preparation will use the additional permissions above; this check does not verify all publishing permissions.</p>
            <a href="https://developers.printify.com/#create-a-personal-access-token" target="_blank" rel="noopener noreferrer">View Printify’s token instructions <span aria-hidden="true">↗</span><span className="visually-hidden"> (opens in a new tab)</span></a>
          </details>
        </>}
        {busy && <div className="store-setup-working">
          <span className="store-setup-spinner" aria-hidden="true" />
          <div role="status" aria-live="polite" aria-atomic="true">
            <h2>{state.step === "validating" ? "Checking your connection…" : state.step === "loading" ? "Finding your stores…" : "Connecting your store…"}</h2>
            <p>{state.step === "validating" ? "Checking store-reading access and looking for your stores." : state.step === "loading" ? "Looking for stores available through this Printify connection." : "Saving the store you selected to your workspace."}</p>
          </div>
          <div className="store-setup-shimmer" aria-hidden="true" />
          <button className="button button--quiet" type="button" onClick={reset}>Cancel</button>
        </div>}
        {state.step === "load_error" && <>
          <h2 ref={focusTarget} tabIndex={-1}>We couldn’t load your stores.</h2>
          <ErrorNotice title="Your token was checked, but the store list didn’t arrive." detail="Your store has not been connected. Try loading the list again." />
          <div className="store-setup-actions"><button className="button button--primary" type="button" onClick={() => { void retryStores(state.validationId); }}>Retry loading stores</button><button className="button button--quiet" type="button" onClick={reset}>Use a different token</button></div>
        </>}
        {state.step === "empty" && <>
          <div className="store-setup-symbol"><StoreIcon /></div>
          <h2 ref={focusTarget} tabIndex={-1}>No eligible stores found.</h2>
          <p className="store-setup-description">There isn’t a supported store available through this connection yet. Check your stores in Printify, or try a token from a different account.</p>
          <div className="store-setup-actions"><button className="button button--primary" type="button" onClick={reset}>Check again</button></div>
        </>}
        {state.step === "choose" && <>
          <p className="eyebrow">Your available stores</p>
          <h2 ref={focusTarget} tabIndex={-1}>Where will your listings live?</h2>
          <p className="store-setup-description">Choose the store Mr. Lister should prepare listings for. Nothing will be published by connecting it.</p>
          {!state.stores.some((store) => store.eligible) && <p role="status">No eligible Etsy stores are available. Check your Etsy connection in Printify, then enter your token again to refresh this list.</p>}
          {state.error !== undefined && <ErrorNotice title={state.error === "selection_required" ? "Choose a store to continue." : "We couldn’t finish connecting."} detail={state.error === "selection_required" ? "Select one of the stores below." : "We couldn’t confirm the connection. Retry the same selection safely."} />}
          <form onSubmit={(event) => { void connect(event); }}>
            <fieldset className="store-setup-choices" aria-describedby={state.error ? "store-setup-error" : undefined}>
              <legend>Choose your store</legend>
              {state.stores.map((store) => <label key={store.id} className={`store-setup-choice${state.selected === store.id ? " is-selected" : ""}`}>
                <input type="radio" name="printify-store" value={store.id} disabled={!store.eligible} checked={state.selected === store.id} onChange={() => setState({ step: "choose", validationId: state.validationId, stores: state.stores, selected: store.id })} />
                <span className="store-setup-choice-icon" aria-hidden="true"><StoreIcon /></span>
                <span><strong>{store.name}</strong><span className="store-setup-channel">{store.salesChannel}{!store.eligible && ` · ${store.disabledReason === "disconnected" ? "Reconnect this store in Printify" : "Etsy stores only"}`}</span></span>
              </label>)}
            </fieldset>
            <div className="store-setup-actions"><button className="button button--primary" type="submit">{state.error === "connection_failed" ? "Try connecting again" : "Connect store"}<ArrowIcon /></button><button className="button button--quiet" type="button" onClick={reset}>Use a different token</button></div>
          </form>
        </>}
        {state.step === "connected" && <div className="store-setup-success">
          <div className="store-setup-success-check" aria-hidden="true">✓</div>
          <p className="eyebrow">{simulated ? "Simulated connection" : "Connection complete"}</p>
          <h2 ref={focusTarget} tabIndex={-1}>{simulated ? "Your sample store is ready." : "Your store is ready."}</h2>
          <p>{simulated ? "Here’s how your connected workspace will look. No real store was connected or changed." : "You’re ready to turn your next design into a listing."}</p>
          <div className="store-setup-connected-store"><StoreIcon /><div><strong>{state.connection.store.name}</strong><span>{state.connection.store.salesChannel}</span></div><span className="store-setup-connected-badge">{simulated ? "Sample" : "Connected"}</span></div>
          <button className="button button--primary" type="button" onClick={() => { if (session.getStatus() === "authenticated") onContinue(state.connection); }}>Enter Mr. Lister <ArrowIcon /></button>
          <p className="store-setup-success-note">{simulated ? "Opens a sample workspace. Uploading and publishing are unavailable in this preview." : "Your next listing starts with your artwork."}</p>
          {simulated && <button className="button button--quiet" type="button" onClick={reset}>Review setup again</button>}
        </div>}
      </div>
      <div className="store-setup-card-footer"><ShieldIcon /><span>{simulated ? "Local preview · No credentials are sent or saved." : "Your connection belongs to your signed-in account."}</span></div>
    </section>
  </div>;
}

function ErrorNotice({ title, detail }: { title: string; detail: string }) {
  return <div className="store-setup-error" id="store-setup-error" role="alert" tabIndex={-1}><strong>{title}</strong><p>{detail}</p></div>;
}
function StoreIcon() {
  return <svg viewBox="0 0 32 32" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 13v14h22V13M4 12l3-7h18l3 7M12 27v-9h8v9M4 12c0 5 6 5 6 0 0 5 6 5 6 0 0 5 6 5 6 0 0 5 6 5 6 0" /><path d="m12 5-2 7m10-7 2 7" /></svg>;
}
function ShieldIcon() {
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6l8-3Z" /><path d="m8 12 3 3 5-6" /></svg>;
}
function ArrowIcon() {
  return <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M3 10h13m-5-5 5 5-5 5" /></svg>;
}
