import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserApiClient } from "./api/client";
import { App } from "./App";
import { OAuthCoordinator } from "./auth/session";
import { judgeSignOutTarget, loadRuntimeConfig } from "./runtime";
import { BrowserPublicationApiClient } from "./publication/api-client";
import { initializeTheme } from "./theme";
import "./styles.css";

initializeTheme();

const rootNode = document.getElementById("root");
if (rootNode === null) throw new Error("Application root is missing");
const root = createRoot(rootNode);

void loadRuntimeConfig().then((config) => {
  const signOutTarget = judgeSignOutTarget(config, window.location.pathname);
  if (signOutTarget !== null) {
    window.location.replace(signOutTarget);
    return;
  }
  const auth = new OAuthCoordinator(config);
  const api = new BrowserApiClient(auth.session);
  const publicationApi = new BrowserPublicationApiClient(auth.session);
  const judgeAccess = config.judge_access === undefined ? undefined : {
    ...(config.judge_access.prepared_job_id === undefined ? {} : { preparedJobId: config.judge_access.prepared_job_id }),
  };
  root.render(<StrictMode><App dependencies={{ auth, api, publicationApi, ...(judgeAccess === undefined ? {} : { judgeAccess }) }} /></StrictMode>);
}).catch(() => {
  root.render(
    <StrictMode>
      <main id="main-content" className="configuration-error">
        <p className="eyebrow">Configuration unavailable</p>
        <h1>The seller workspace cannot start safely.</h1>
        <p>Deployment configuration is missing or invalid. No seller data was requested.</p>
      </main>
    </StrictMode>,
  );
});
