import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserApiClient } from "./api/client";
import { App } from "./App";
import { OAuthCoordinator } from "./auth/session";
import { loadRuntimeConfig } from "./runtime";
import { BrowserPublicationApiClient } from "./publication/api-client";
import "./styles.css";

const rootNode = document.getElementById("root");
if (rootNode === null) throw new Error("Application root is missing");
const root = createRoot(rootNode);

void loadRuntimeConfig().then((config) => {
  const auth = new OAuthCoordinator(config);
  const api = new BrowserApiClient(auth.session);
  const publicationApi = new BrowserPublicationApiClient(auth.session);
  root.render(<StrictMode><App dependencies={{ auth, api, publicationApi }} /></StrictMode>);
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
