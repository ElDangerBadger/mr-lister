import { useAppDependencies } from "../app-context";
import { useSignIn } from "../auth/sign-in";
import sampleArtwork from "../assets/judge-sample-artwork.png";

export function JudgeAccessPage() {
  const { judgeAccess } = useAppDependencies();
  const { startSignIn } = useSignIn();
  const preparedJobId = judgeAccess?.preparedJobId;

  return (
    <div className="page judge-access-page">
      <section className="judge-access-intro" aria-labelledby="judge-access-heading">
        <p className="eyebrow">Welcome, judges</p>
        <h1 id="judge-access-heading">Try the full listing journey.</h1>
        <p className="judge-access-lede">Start with artwork. Shape the listing. Decide when it’s ready for your storefront.</p>
        <p className="judge-access-sign-in-note">Continue to secure sign-in with the judge account provided to you.</p>
        <div className="judge-access-actions">
          <button className="button button--primary" type="button" onClick={() => { startSignIn("/"); }}>
            Sign in with judge access <span aria-hidden="true">↗</span>
          </button>
          {preparedJobId !== undefined && <button className="button" type="button" onClick={() => {
            startSignIn(`/jobs/${encodeURIComponent(preparedJobId)}`);
          }}>
            Review prepared example
          </button>}
        </div>
        {preparedJobId !== undefined && <p className="judge-access-example-note">The prepared example opens after sign-in. You can also start a fresh upload.</p>}
        <ol className="judge-journey" aria-label="The listing journey">
          <li>
            <span aria-hidden="true">01</span>
            <div><h2>Upload</h2><p>Use the sample artwork or bring your own design.</p></div>
          </li>
          <li>
            <span aria-hidden="true">02</span>
            <div><h2>Review</h2><p>Follow preparation, edit the copy, and check the product and pricing.</p></div>
          </li>
          <li>
            <span aria-hidden="true">03</span>
            <div><h2>Publish</h2><p>Approve the saved listing, then confirm before it goes live.</p></div>
          </li>
        </ol>
      </section>
      <section className="judge-sample-card" aria-labelledby="judge-sample-heading">
        <div className="judge-sample-preview">
          <img src={sampleArtwork} alt="Sample artwork: a teal wave and mountain beneath an orange sunset" width="1254" height="1254" />
          <span className="judge-sample-label">Ready to upload</span>
        </div>
        <div className="judge-sample-copy">
          <div>
            <p className="eyebrow">Your starting point</p>
            <h2 id="judge-sample-heading">A design to try.</h2>
            <p>Download the sample, sign in, and add it to the upload area. Preparation begins when you choose Prepare 1 listing.</p>
          </div>
          <a className="button" href={sampleArtwork} download="mr-lister-sample-artwork.png">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M4 17v3h16v-3" /></svg>
            Download sample artwork
          </a>
          <p className="judge-sample-meta">PNG · 1254 × 1254 pixels · 2.5 MB</p>
        </div>
      </section>
    </div>
  );
}
