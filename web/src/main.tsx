import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { mountApplication } from "./bootstrap";
import { judgeSignOutTarget, loadRuntimeConfig } from "./runtime";
import { initializeTheme } from "./theme";
import { captureJudgeInvitation } from "./auth/judge-session";
import "./styles.css";

const judgeInvitation = captureJudgeInvitation();
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
  mountApplication(root, config, judgeInvitation);
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
