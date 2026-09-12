import { useEffect, useRef } from "react";
import { BrowserRouter, Route, Routes, useLocation } from "react-router-dom";
import { AppContext, type AppDependencies } from "./app-context";
import { useSessionStatus } from "./auth/use-session";
import { SignInProvider, useSignIn } from "./auth/sign-in";
import { AuthCallbackPage } from "./pages/AuthCallbackPage";
import { HomePage } from "./pages/HomePage";
import { JobReviewPage } from "./pages/JobReviewPage";
import { UploadPage } from "./pages/UploadPage";
import { UploadProvider } from "./upload/upload-context";
import { useUpload } from "./upload/upload-context";
import mrListerIcon from "./assets/mr-lister-icon.png";
import { ThemeControl } from "./components/ThemeControl";
import { WorkspaceLink, WorkspaceNavigationProvider } from "./navigation/WorkspaceNavigation";
import { BatchWorkspaceProvider } from "./navigation/BatchWorkspace";
import "./styles.css";

export function App({ dependencies }: { dependencies: AppDependencies }) {
  return (
    <BrowserRouter>
      <AppRoutes dependencies={dependencies} />
    </BrowserRouter>
  );
}

export function AppRoutes({ dependencies }: { dependencies: AppDependencies }) {
  const status = useSessionStatus(dependencies.auth.session);
  return (
    <AppContext.Provider value={dependencies}>
      <SignInProvider>
      <WorkspaceNavigationProvider>
      <UploadProvider api={dependencies.api}>
      <BatchWorkspaceProvider>
        <div className="app-shell">
          <RouteFocusManager />
          <a className="skip-link" href="#main-content">Skip to main content</a>
          <header className="site-header">
            <div className="site-header-inner">
              <WorkspaceLink className="brand" to="/" aria-label="Mr. Lister seller review home">
                <img className="brand-icon" src={mrListerIcon} alt="" width="56" height="56" />
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
          </header>
          <main id="main-content" tabIndex={-1}>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/auth/callback" element={<AuthCallbackPage />} />
              <Route path="/jobs/:jobId" element={<RequireSession status={status}><JobReviewPage /></RequireSession>} />
              <Route path="/uploads/:uploadId" element={<RequireSession status={status}><UploadPage /></RequireSession>} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </main>
          <footer>
            Made for your next great listing. You review, approve, and confirm before anything is published.
          </footer>
        </div>
      </BatchWorkspaceProvider>
      </UploadProvider>
      </WorkspaceNavigationProvider>
      </SignInProvider>
    </AppContext.Provider>
  );
}

function RouteFocusManager() {
  const location = useLocation();
  const mounted = useRef(false);
  useEffect(() => {
    document.title = routeTitle(location.pathname);
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const timeout = window.setTimeout(() => document.getElementById("main-content")?.focus(), 0);
    return () => window.clearTimeout(timeout);
  }, [location.pathname]);
  return null;
}

function routeTitle(pathname: string): string {
  if (pathname === "/") return "Uploads | Mr. Lister";
  if (pathname === "/auth/callback") return "Secure sign-in | Mr. Lister";
  if (pathname.startsWith("/jobs/")) return "Seller review | Mr. Lister";
  if (pathname.startsWith("/uploads/")) return "Private upload | Mr. Lister";
  return "Not found | Mr. Lister";
}

function SessionControls({ status, dependencies }: { status: "anonymous" | "authenticated"; dependencies: AppDependencies }) {
  const upload = useUpload();
  const location = useLocation();
  const { startSignIn } = useSignIn();
  return (
    <div className="session-controls">
      <span className={`session-dot session-dot--${status}`} aria-hidden="true" />
      <span>{status === "authenticated" ? "Signed in" : "Signed out"}</span>
      {status === "anonymous" && <button className="button button--quiet" type="button" onClick={() => { startSignIn(location.pathname); }}>Sign in</button>}
      {status === "authenticated" && (
        <button className="button button--quiet" type="button" onClick={() => { upload.reset(); dependencies.auth.signOut(); }}>
          Sign out
        </button>
      )}
    </div>
  );
}

function RequireSession({ status, children }: { status: "anonymous" | "authenticated"; children: React.ReactNode }) {
  const { startSignIn } = useSignIn();
  const location = useLocation();
  if (status === "authenticated") return children;
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
