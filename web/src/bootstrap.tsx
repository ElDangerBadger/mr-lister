import { StrictMode } from "react";
import { flushSync } from "react-dom";
import type { Root } from "react-dom/client";
import { BrowserApiClient } from "./api/client";
import { App } from "./App";
import { OAuthCoordinator } from "./auth/session";
import { judgeWorkspacePath, resolveJudgeWorkspace } from "./auth/workspace";
import type { RuntimeConfig } from "./contracts";
import { BrowserPublicationApiClient } from "./publication/api-client";

/** Keep one in-memory session while selecting the authenticated workspace. */
export function mountApplication(root: Root, config: RuntimeConfig): void {
  const auth = new OAuthCoordinator(config, undefined, undefined, undefined, undefined, {
    resolveWorkspace: resolveJudgeWorkspace,
    activateWorkspace: (next, returnPath) => {
      window.history.replaceState(null, "", judgeWorkspacePath(returnPath));
      // Retire the old router before exposing the session to authenticated pages.
      flushSync(() => { renderApplication(next); });
    },
  });
  const api = new BrowserApiClient(auth.session);
  const publicationApi = new BrowserPublicationApiClient(auth.session);
  function renderApplication(runtime: RuntimeConfig) {
    const judgeAccess = runtime.judge_access === undefined ? undefined : {
      ...(runtime.judge_access.prepared_job_id === undefined ? {} : { preparedJobId: runtime.judge_access.prepared_job_id }),
    };
    root.render(<StrictMode><App key={judgeAccess === undefined ? "seller" : "judge"} dependencies={{ auth, api, publicationApi, ...(judgeAccess === undefined ? {} : { judgeAccess }) }} /></StrictMode>);
  }
  renderApplication(config);
}
