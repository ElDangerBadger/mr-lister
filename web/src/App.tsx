import { useEffect, useRef } from "react";
import { BrowserRouter, Link, Route, Routes, useLocation } from "react-router-dom";
import { AppContext, useAppDependencies, type AppDependencies } from "./app-context";
import { useSessionStatus } from "./auth/use-session";
import { AuthCallbackPage } from "./pages/AuthCallbackPage";
import { HomePage } from "./pages/HomePage";
import { JobReviewPage } from "./pages/JobReviewPage";
import { UploadPage } from "./pages/UploadPage";
import { UploadProvider } from "./upload/upload-context";
import { useUpload } from "./upload/upload-context";
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
      <UploadProvider api={dependencies.api}>
        <div className="app-shell">
          <RouteFocusManager />
          <a className="skip-link" href="#main-content">Skip to main content</a>
          <header className="site-header">
            <Link className="brand" to="/" aria-label="Mr. Lister seller review home">
              <span className="brand-mark" aria-hidden="true">ML</span>
              <span>Mr. Lister</span>
            </Link>
            <SessionControls status={status} dependencies={dependencies} />
          </header>
          <div className="authority-banner" role="note">
            <span aria-hidden="true">●</span>
            <strong>Nothing publishes without explicit seller confirmation</strong>
          </div>
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
            Drafts remain private until a seller explicitly approves and confirms publication. Mr. Lister cannot order or fulfill products.
          </footer>
        </div>
      </UploadProvider>
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
  return (
    <div className="session-controls">
      <span className={`session-dot session-dot--${status}`} aria-hidden="true" />
      <span>{status === "authenticated" ? "Signed in" : "Signed out"}</span>
      {status === "authenticated" && (
        <button className="button button--quiet" type="button" onClick={() => { upload.reset(); dependencies.auth.signOut(); }}>
          Sign out
        </button>
      )}
    </div>
  );
}

function RequireSession({ status, children }: { status: "anonymous" | "authenticated"; children: React.ReactNode }) {
  const { auth } = useAppDependencies();
  const location = useLocation();
  if (status === "authenticated") return children;
  return (
    <section className="page narrow-page">
      <p className="eyebrow">Secure session</p>
      <h1>Restore your seller session.</h1>
      <p>Your protected route is preserved. Cognito may restore its managed sign-in session without asking for credentials again.</p>
      <button className="button button--primary" type="button" onClick={() => { void auth.startSignIn(location.pathname); }}>Continue securely</button>
    </section>
  );
}

function NotFound() {
  return (
    <section className="page narrow-page">
      <p className="eyebrow">Not found</p>
      <h1>That seller workspace does not exist.</h1>
      <p><Link to="/">Return to uploads</Link></p>
    </section>
  );
}
