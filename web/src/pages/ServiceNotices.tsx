import { useEffect } from "react";
import { Link } from "react-router-dom";
import { useAppDependencies } from "../app-context";
import { ThemeControl } from "../components/ThemeControl";
import { WorkspaceLink } from "../navigation/WorkspaceNavigation";
import { SERVICE_NOTICE_VERSION, areServiceNoticesReviewed, serviceSupportEmail } from "../service-notices";
import "./service-notices.css";

export function ServiceNoticeLinks({ newTab = false }: { newTab?: boolean }) {
  const destination = newTab ? { target: "_blank", rel: "noopener noreferrer" } : {};
  return <span className="service-notice-links">
    <WorkspaceLink to="/privacy" {...destination}>Privacy{newTab && <span className="visually-hidden"> (opens in a new tab)</span>}</WorkspaceLink>
    <WorkspaceLink to="/terms" {...destination}>Terms{newTab && <span className="visually-hidden"> (opens in a new tab)</span>}</WorkspaceLink>
  </span>;
}

/** Public information only: no account, upload, history or publication providers. */
export function ServiceNoticePage({ kind, account }: { kind: "privacy" | "terms"; account: boolean }) {
  const { accountConfig, judgeAccess } = useAppDependencies();
  const reviewed = areServiceNoticesReviewed(accountConfig?.noticeVersion, accountConfig?.supportEmail);
  const supportEmail = serviceSupportEmail(accountConfig?.supportEmail);
  const title = kind === "privacy" ? "Your privacy, in plain language." : "A few things to agree on.";
  useEffect(() => { document.title = `${kind === "privacy" ? "Privacy" : "Terms"} | Mr. Lister`; document.getElementById("main-content")?.focus(); }, [kind]);
  return <div className="app-shell service-notice-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="site-header"><div className="site-header-inner"><Link className="brand" to="/" reloadDocument={judgeAccess !== undefined} aria-label="Mr. Lister home"><span>Mr. Lister<span className="service-notice-brand-dot">.</span></span></Link><div className="header-controls"><ThemeControl /><Link className="button button--quiet" to={account ? "/store-setup" : "/"} reloadDocument={judgeAccess !== undefined}>Back to Mr. Lister</Link></div></div></header>
    <main id="main-content" tabIndex={-1} className="page service-notice-page">
      <div className="service-notice-intro"><p className="eyebrow">{kind === "privacy" ? "Privacy notice" : "Service terms"}</p><h1>{title}</h1><p>{kind === "privacy" ? "What your workspace needs, where it goes, and the choices you keep." : "Your artwork and your store stay yours. Here’s how Mr. Lister helps."}</p></div>
      <div className="service-notice-layout"><aside className="service-notice-summary"><p className="service-notice-draft">{reviewed ? "Current notice" : "Draft for operator review"}</p><p>Version {SERVICE_NOTICE_VERSION}</p><p>{reviewed ? "How Mr. Lister handles your account and store connection. Contact us below with questions about these notices." : "These notices describe the account and store connection being prepared for release. Public availability remains staged while the operator reviews the notices and contact details."}</p><nav aria-label="Service notices"><ServiceNoticeLinks /></nav></aside>
        <article className="service-notice-body">
          {kind === "privacy" ? <PrivacyText /> : <TermsText />}
          <section id="contact"><h2>Contact and account requests</h2>{supportEmail === null ? <p>Public contact details are being finalized. The operator must add a support address before opening account and store connections to the public.</p> : <p>For questions about your account, connection, or a data access or deletion request, contact <a href={`mailto:${supportEmail}`}>{supportEmail}</a>. Please don’t email passwords or connection tokens.</p>}</section>
        </article>
      </div>
    </main>
    <footer className="service-notice-footer"><span>Made for your next great listing.</span><ServiceNoticeLinks /></footer>
  </div>;
}

function PrivacyText() {
  return <>
    <section><h2>Your account and sign-in</h2><p>Amazon Cognito handles account sign-up and sign-in, including email verification and authenticator-app codes. Mr. Lister uses your account identifier to keep your workspace separate. Your password and authenticator secret aren’t entered into the listing app.</p></section>
    <section><h2>Your Printify connection</h2><p>When you authorize a connection, the token you enter is sent over HTTPS to the Mr. Lister service and stored in encrypted AWS Secrets Manager storage. The browser form clears the token when you submit it; the app doesn’t save it in browser storage or return it in store summaries.</p><p>We read the stores available to that token so you can choose a connected Etsy store. The saved connection associates that store with your account. Listing preparation uses the connection to upload artwork and create or update draft products. Publishing requires your approval and confirmation.</p><p>This listing workflow doesn’t request order or customer data and doesn’t install Printify webhooks. A token with broader permissions still grants those permissions at Printify; use a dedicated token with only the access described in setup.</p></section>
    <section><h2>Your artwork and listing data</h2><p>Your uploaded artwork, listing drafts, store references, and workflow status are processed and stored on AWS. To prepare a listing, artwork and relevant listing information are processed with the Gemma model hosted on AWS and sent to Printify for product preparation and mockups. The listing you approve can then be sent through Printify to your selected storefront.</p><p>The service keeps technical records such as request identifiers, status, and error codes to operate and troubleshoot the workflow. Connection tokens are excluded from application responses and operational log messages.</p></section>
    <section><h2>Your choices</h2><p>You can stop future Printify access by revoking the dedicated token in Printify. Signing out ends your browser session but does not revoke the saved Printify connection. Clearing recent activity hides it from your recent list; it does not delete products from Printify or Etsy.</p><p>Your browser may retain your display-theme preference. For account sign-in, access and refresh tokens are held in browser memory during the session. A temporary sign-in transaction is used to complete secure authentication. Invited judge access uses a separate secure session cookie.</p></section>
  </>;
}

function TermsText() {
  return <>
    <section><h2>Use an account and store you’re authorized to manage</h2><p>Connect only a Printify account and Etsy store you own or have permission to manage. Keep your sign-in credentials and authenticator secure. Use a dedicated Printify personal access token and authorize its use through the connection form.</p></section>
    <section><h2>You keep ownership and control</h2><p>You retain your rights in your artwork and products. By submitting artwork and authorizing a store connection, you permit Mr. Lister and the service providers described in the privacy notice to process that material as needed to prepare, revise, and publish the listings you request.</p><p>Submit only material you have the right to use and sell. You remain responsible for your listing content, product claims, pricing, and compliance with your storefront’s rules.</p></section>
    <section><h2>Review before publishing</h2><p>Mr. Lister helps prepare copy, product mockups, and estimates. Generated content and estimates can contain mistakes. Review the artwork placement, listing text, variants, pricing, and current store costs before approving a listing.</p><p>Connecting a store does not publish a listing. Mr. Lister prepares drafts; you approve and confirm publication. A publication that has already been submitted may finish even if you later close the browser or sign out.</p></section>
    <section><h2>Printify and your storefront</h2><p>Printify and Etsy are separate services with their own terms, fees, availability, and account requirements. Using a personal access token does not mean Printify has approved or endorsed Mr. Lister. You remain responsible for your accounts and any charges resulting from the listings you choose to publish.</p><p>The Mr. Lister operator is responsible for this application, its support, and its access to and handling of your store data. Printify does not provide support for installing or using Mr. Lister and is not responsible for faults in this application or harm arising from its use.</p></section>
    <section><h2>Stopping or changing access</h2><p>You can revoke the dedicated token in Printify to stop future access through that token. Existing products remain in your Printify and Etsy accounts; revoking access or signing out does not delete them. Contact the operator for help with your Mr. Lister account or stored data.</p><p>Features may be unavailable while setup is staged or a connected service cannot be reached. Check the current listing status before repeating an action when its result is uncertain.</p></section>
  </>;
}
