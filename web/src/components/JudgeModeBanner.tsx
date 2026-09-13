import { useAppDependencies } from "../app-context";
import sampleArtwork from "../assets/judge-sample-artwork.png";
import { WorkspaceLink } from "../navigation/WorkspaceNavigation";

export function JudgeModeBanner({ authenticated }: { authenticated: boolean }) {
  const { judgeAccess } = useAppDependencies();
  if (judgeAccess === undefined) return null;

  return (
    <aside className="judge-banner" aria-label="Judge walkthrough">
      <div className="judge-banner-inner">
        <div className="judge-banner-copy">
          <strong>Judge walkthrough</strong>
          <p>This walkthrough uses the connected live store. Publishing creates a real Etsy listing after your final confirmation.</p>
        </div>
        {authenticated && <nav className="judge-resources" aria-label="Judge resources">
          <a href={sampleArtwork} download="mr-lister-sample-artwork.png">Download sample artwork</a>
          {judgeAccess.preparedJobId !== undefined && <WorkspaceLink to={`/jobs/${encodeURIComponent(judgeAccess.preparedJobId)}`}>
            Review prepared example
          </WorkspaceLink>}
        </nav>}
      </div>
    </aside>
  );
}
