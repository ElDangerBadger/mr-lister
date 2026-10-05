import { useEffect, useMemo, useRef } from "react";
import { BrowserRouter, Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AppContext, useAppDependencies, type AppDependencies } from "./app-context";
import { useSessionStatus } from "./auth/use-session";
import { SignInProvider, useSignIn } from "./auth/sign-in";
import { AuthCallbackPage } from "./pages/AuthCallbackPage";
import { HomePage } from "./pages/HomePage";
import { LandingFooter, LandingHeader } from "./pages/LandingPage";
import { JobReviewPage } from "./pages/JobReviewPage";
import { UploadPage } from "./pages/UploadPage";
import { UploadProvider } from "./upload/upload-context";
import { useUpload } from "./upload/upload-context";
import mrListerBanner from "./assets/mr-lister-dark.png";
import { ThemeControl } from "./components/ThemeControl";
import { WorkspaceLink, WorkspaceNavigationProvider } from "./navigation/WorkspaceNavigation";
import { BatchWorkspaceProvider } from "./navigation/BatchWorkspace";
import { DraftCancellationProvider } from "./navigation/DraftCancellation";
import { JudgeModeBanner } from "./components/JudgeModeBanner";
import { JudgeSessionCoordinator } from "./auth/judge-session";
import { JudgeSessionEntry } from "./auth/JudgeSessionEntry";
import { AccountSetupPage, useAccountIdentity, useAccountSetup, useAccountWorkflowToken, type AccountSetupController } from "./auth/AccountSetupPage";
import type { ConnectedStore } from "./store-setup/connection-adapter";
import { ServiceNoticeLinks, ServiceNoticePage } from "./pages/ServiceNotices";
import { areServiceNoticesReviewed } from "./service-notices";
import "./styles.css";
import "./landing-fonts.css";
import "./landing.css";
import "./judge-access.css";

export function App({ dependencies }: { dependencies: AppDependencies }) {
  return (
    <BrowserRouter basename={dependencies.judgeAccess === undefined ? "/" : "/judge"}>
      <AppRoutes dependencies={dependencies} />
    </BrowserRouter>
  );
}

export function AppRoutes({ dependencies }: { dependencies: AppDependencies }) {
  const identity = useAccountIdentity(dependencies.auth.session, dependencies.judgeAccess === undefined ? dependencies.accountConfig : undefined);
  const location = useLocation();
  if (location.pathname === "/privacy" || location.pathname === "/terms") return <AppContext.Provider value={dependencies}><ServiceNoticePage kind={location.pathname === "/privacy" ? "privacy" : "terms"} account={identity !== null} /></AppContext.Provider>;
  if (identity !== null) return <AccountBoundary key={identity} identity={identity} dependencies={dependencies} />;
  return <WorkflowRoutes dependencies={dependencies} />;
}

function AccountBoundary({ identity, dependencies }: { identity: string; dependencies: AppDependencies }) {
  const setup = useAccountSetup(identity, dependencies.accountApi, dependencies.auth.session);
  const workflowToken = useAccountWorkflowToken(dependencies.auth.session, dependencies.accountConfig, identity);
  const location = useLocation();
  const navigate = useNavigate();
  const store = setup.value?.state === "ready" && setup.value.connection_method === "personal_token" ? setup.value.store : null;
  const bindingId = store?.shop_binding_id;
  const setupVersion = setup.value?.record_version;
  const enabled = dependencies.accountConfig?.connectedWorkflow === true && dependencies.accountConfig.connectionMethod === "personal_token" && areServiceNoticesReviewed(dependencies.accountConfig.noticeVersion, dependencies.accountConfig.supportEmail);
  const factory = dependencies.createAccountWorkflowApi;
  const workflowApi = useMemo(() => enabled && bindingId !== undefined && setupVersion !== undefined && factory !== undefined ? factory({ shop_binding_id: bindingId, expected_setup_version: setupVersion }) : null, [enabled, bindingId, setupVersion, factory]);
  const canOpen = workflowToken && workflowApi !== null && !setup.loading && setup.error === null;
  const workflowDependencies = useMemo(() => workflowApi === null ? dependencies : { ...dependencies, api: workflowApi }, [dependencies, workflowApi]);
  function continueToWorkspace(connection: ConnectedStore) {
    if (!enabled || !workflowToken) return;
    setup.accept(connection.setup);
    void navigate("/", { replace: true });
  }
  const authenticatedCallback = location.pathname === "/auth/callback";
  if (canOpen && location.pathname !== "/store-setup" && !authenticatedCallback) return <WorkflowRoutes dependencies={workflowDependencies} />;
  return <AppContext.Provider value={dependencies}><SignInProvider><AccountWorkspace identity={identity} dependencies={dependencies} controller={setup} onContinue={continueToWorkspace} canonicalize={authenticatedCallback || !setup.loading && !canOpen} /></SignInProvider></AppContext.Provider>;
}

function WorkflowRoutes({ dependencies }: { dependencies: AppDependencies }) {
  const status = useSessionStatus(dependencies.auth.session);
  const location = useLocation();
  const judgeMode = dependencies.judgeAccess !== undefined;
  const landing = !judgeMode && status === "anonymous" && location.pathname === "/";
  return (
    <AppContext.Provider value={dependencies}>
      <SignInProvider>
      <WorkspaceNavigationProvider>
      <UploadProvider api={dependencies.api}>
      <DraftCancellationProvider>
      <BatchWorkspaceProvider>
        <div className={landing ? "app-shell app-shell--landing" : "app-shell"}>
          <RouteFocusManager status={status} judgeMode={judgeMode} />
          <a className="skip-link" href="#main-content">Skip to main content</a>
          {landing ? <LandingHeader /> : <header className="site-header">
            <div className="site-header-inner">
              <WorkspaceLink className="brand" to="/" aria-label="Mr. Lister seller review home">
                <img className="brand-icon" src={mrListerBanner} alt="" width="64" height="64" />
                <span>Mr. Lister</span>
              </WorkspaceLink>
              <div className="header-controls">
                {status === "authenticated" && <WorkspaceLink className="button button--quiet header-dashboard" to="/" aria-label="Dashboard">
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>
                  <span>Dashboard</span>
                </WorkspaceLink>}
                <ThemeControl />
                <SessionControls status={status} dependencies={dependencies} />
              </div>
            </div>
          </header>}
          <JudgeModeBanner authenticated={status === "authenticated"} />
          <main id="main-content" tabIndex={-1}>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/auth/callback" element={<AuthCallbackPage />} />
              {!judgeMode && <Route path="/store-setup" element={<RequireSession status={status}><LegacyStoreSetup /></RequireSession>} />}
              <Route path="/jobs/:jobId" element={<RequireSession status={status}><JobReviewPage /></RequireSession>} />
              <Route path="/uploads/:uploadId" element={<RequireSession status={status}><UploadPage /></RequireSession>} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </main>
          {landing ? <LandingFooter /> : <footer className="service-notice-footer">
            <span>Made for your next great listing. You review, approve, and confirm before anything is published.</span><ServiceNoticeLinks />
          </footer>}
        </div>
      </BatchWorkspaceProvider>
      </DraftCancellationProvider>
      </UploadProvider>
      </WorkspaceNavigationProvider>
      </SignInProvider>
    </AppContext.Provider>
  );
}

function AccountWorkspace({ identity, dependencies, controller, onContinue, canonicalize }: { identity: string; dependencies: AppDependencies; controller: AccountSetupController; onContinue: (connection: ConnectedStore) => void; canonicalize: boolean }) {
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    document.title = "Your account | Mr. Lister";
    if (canonicalize && location.pathname !== "/store-setup") void navigate("/store-setup", { replace: true });
  }, [location.pathname, navigate, canonicalize]);
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="site-header"><div className="site-header-inner">
      <Link className="brand" to="/store-setup" aria-label="Mr. Lister account home"><img className="brand-icon" src={mrListerBanner} alt="" width="64" height="64" /><span>Mr. Lister</span></Link>
      <div className="header-controls"><ThemeControl /><div className="session-controls"><span className="session-dot session-dot--authenticated" aria-hidden="true" /><span>Signed in</span><button className="button button--quiet" type="button" onClick={() => { dependencies.auth.signOut(); void navigate("/", { replace: true }); }}>Sign out</button></div></div>
    </div></header>
    <main id="main-content" tabIndex={-1}><AccountSetupPage key={identity} identity={identity} controller={controller} onContinue={onContinue} /></main>
    <footer className="service-notice-footer"><span>Made for your next great listing.</span><ServiceNoticeLinks /></footer>
  </div>;
}

function LegacyStoreSetup() {
  return <section className="page narrow-page"><p className="eyebrow">Your workspace</p><h1>Your store is already managed.</h1><p>Continue to your existing workspace to work on your listings.</p><WorkspaceLink className="button button--primary" to="/">Open workspace</WorkspaceLink></section>;
}

function RouteFocusManager({ status, judgeMode }: { status: "anonymous" | "authenticated"; judgeMode: boolean }) {
  const location = useLocation();
  const mounted = useRef(false);
  useEffect(() => {
    document.title = judgeMode && location.pathname === "/" && status === "anonymous"
      ? "Judge access | Mr. Lister" : routeTitle(location.pathname, status);
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const timeout = window.setTimeout(() => document.getElementById("main-content")?.focus(), 0);
    return () => window.clearTimeout(timeout);
  }, [location.pathname, status, judgeMode]);
  return null;
}

function routeTitle(pathname: string, status: "anonymous" | "authenticated"): string {
  if (pathname === "/") return status === "anonymous" ? "Mr. Lister — Your next listing, made simpler." : "Uploads | Mr. Lister";
  if (pathname === "/auth/callback") return "Secure sign-in | Mr. Lister";
  if (pathname === "/store-setup") return "Your account | Mr. Lister";
  if (pathname.startsWith("/jobs/")) return "Seller review | Mr. Lister";
  if (pathname.startsWith("/uploads/")) return "Private upload | Mr. Lister";
  return "Not found | Mr. Lister";
}

function SessionControls({ status, dependencies }: { status: "anonymous" | "authenticated"; dependencies: AppDependencies }) {
  const upload = useUpload();
  const location = useLocation();
  const { startSignIn } = useSignIn();
  const navigate = useNavigate();
  const linkEntry = dependencies.auth instanceof JudgeSessionCoordinator;
  return (
    <div className="session-controls">
      <span className={`session-dot session-dot--${status}`} aria-hidden="true" />
      <span>{status === "authenticated" ? "Signed in" : "Signed out"}</span>
      {status === "anonymous" && !linkEntry && <button className="button button--quiet" type="button" onClick={() => { startSignIn(location.pathname); }}>Sign in</button>}
      {status === "authenticated" && (
        <button className="button button--quiet" type="button" onClick={() => { upload.reset(); dependencies.auth.signOut(); if (linkEntry) void navigate("/", { replace: true }); }}>
          Sign out
        </button>
      )}
    </div>
  );
}

function RequireSession({ status, children }: { status: "anonymous" | "authenticated"; children: React.ReactNode }) {
  const { startSignIn } = useSignIn();
  const location = useLocation();
  const { auth } = useAppDependencies();
  if (status === "authenticated") return children;
  if (auth instanceof JudgeSessionCoordinator) return <section className="page narrow-page">
    <p className="eyebrow">Judge workspace</p>
    <h1>Continue your listing journey.</h1>
    <JudgeSessionEntry auth={auth} returnPath={location.pathname} />
  </section>;
  return (
    <section className="page narrow-page">
      <p className="eyebrow">Secure session</p>
      <h1>Restore your seller session.</h1>
      <p>Sign in to continue where you left off. Your artwork and listings are private to your account.</p>
      <button className="button button--primary" type="button" onClick={() => { startSignIn(location.pathname); }}>Continue securely</button>
    </section>
  );
}

function NotFound() {
  return (
    <section className="page narrow-page">
      <p className="eyebrow">Not found</p>
      <h1>That seller workspace does not exist.</h1>
      <p><WorkspaceLink to="/">Return to uploads</WorkspaceLink></p>
    </section>
  );
}
