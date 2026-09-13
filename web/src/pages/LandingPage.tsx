import { useSignIn } from "../auth/sign-in";
import { ThemeControl } from "../components/ThemeControl";
import { WorkspaceLink } from "../navigation/WorkspaceNavigation";
import mrListerIcon from "../assets/mr-lister-icon.png";
import mrListerDark from "../assets/mr-lister-dark.png";

export function LandingHeader() {
  const { startSignIn, error } = useSignIn();
  return (
    <header className="landing-header">
      <div className="landing-container landing-header-inner">
        <WorkspaceLink className="landing-brand" to="/" aria-label="Mr. Lister home">
          <span className="landing-brand-name">Mr. Lister<span className="landing-brand-dot">.</span></span>
          <span className="landing-brand-description">Your listing assistant</span>
        </WorkspaceLink>
        <nav className="landing-navigation" aria-label="Main navigation">
          <a className="landing-how-link" href="#how-it-works">How it works</a>
          <ThemeControl />
          <button className="landing-header-signin" type="button" onClick={() => { startSignIn("/"); }}>
            Sign in <ArrowIcon />
          </button>
        </nav>
      </div>
      {error !== null && <p className="landing-signin-error landing-container" role="alert">{error}</p>}
    </header>
  );
}

export function LandingPage() {
  const { startSignIn, error } = useSignIn();
  return (
    <>
      <section className="landing-welcome landing-container" aria-labelledby="welcome-heading">
        <div className="landing-welcome-copy">
          <p className="landing-eyebrow">A HELPING HAND FOR SHOP OWNERS</p>
          <h1 id="welcome-heading">Your artwork.<br /> Your next listing<span className="landing-accent">.</span></h1>
          <p className="landing-intro">You have a shop to run.<br /> Let Mr. Lister help with the listings.</p>
          <p className="landing-description">Turn your artwork into listing copy, product mockups, and estimated proceeds. Review the details, make it yours, and publish when you’re ready.</p>
          <div className="landing-approval-note">
            <span className="landing-approval-icon">
              <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3 4.5 6v5.5c0 4.6 7.5 9 7.5 9s7.5-4.4 7.5-9V6L12 3Z" /><path d="m8.5 11.5 2.5 2.5 4.5-5" /></svg>
            </span>
            <span>Nothing goes live without your approval.</span>
          </div>
        </div>
        <section className="landing-workspace-card" aria-labelledby="workspace-heading">
          <div className="landing-workspace-label">
            <svg aria-hidden="true" viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2" /></svg>
            <span>YOUR PRIVATE WORKSPACE</span>
          </div>
          <img className="landing-mascot landing-mascot--light" src={mrListerIcon} width="360" height="360" alt="Mr. Lister, the friendly price-tag character, holding a product listing" fetchPriority="high" />
          <img className="landing-mascot landing-mascot--dark" src={mrListerDark} width="360" height="360" alt="Mr. Lister, the friendly price-tag character, holding a product listing" fetchPriority="high" />
          <h2 id="workspace-heading">Let’s get to work.</h2>
          <p>Create your next listing, or pick up<br className="landing-desktop-break" /> where you left off.</p>
          <button className="landing-primary-button" type="button" onClick={() => { startSignIn("/"); }}>
            Open seller workspace <ArrowIcon />
          </button>
          <p className="landing-signin-note">Sign in with the account from your invitation.</p>
          {error !== null && <p className="landing-signin-error" role="alert">{error}</p>}
        </section>
      </section>
      <section className="landing-how-section" id="how-it-works" aria-labelledby="how-heading">
        <div className="landing-container">
          <div className="landing-section-heading">
            <div>
              <p className="landing-eyebrow">A SIMPLE, FAMILIAR PROCESS</p>
              <h2 id="how-heading">From artwork to storefront.</h2>
            </div>
            <p>Mr. Lister handles the preparation.<br /> You stay in control.</p>
          </div>
          <ol className="landing-steps">
            <li><span className="landing-step-number">01</span><div><h3>Upload your artwork</h3><p>Start with a design you want to offer in your shop.</p></div></li>
            <li><span className="landing-step-number">02</span><div><h3>Review and refine</h3><p>Check the mockups, edit the copy, and review estimated proceeds.</p></div></li>
            <li><span className="landing-step-number">03</span><div><h3>Approve and publish</h3><p>Make sure it’s right. Then confirm the listing for your store.</p></div></li>
          </ol>
        </div>
      </section>
    </>
  );
}

export function LandingFooter() {
  const { startSignIn, error } = useSignIn();
  return (
    <footer className="landing-footer landing-container">
      <WorkspaceLink className="landing-footer-brand" to="/">Mr. Lister.</WorkspaceLink>
      <p>Made for your next great listing.</p>
      <button className="landing-footer-signin" type="button" onClick={() => { startSignIn("/"); }}>Open seller workspace</button>
      {error !== null && <p className="landing-signin-error" role="alert">{error}</p>}
    </footer>
  );
}

function ArrowIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 12h14m-5-5 5 5-5 5" /></svg>;
}
