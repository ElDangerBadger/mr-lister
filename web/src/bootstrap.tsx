import { StrictMode } from "react";
import { flushSync } from "react-dom";
import type { Root } from "react-dom/client";
import { BrowserApiClient } from "./api/client";
import { App } from "./App";
import { OAuthCoordinator } from "./auth/session";
import { captureJudgeInvitation, JudgeSessionCoordinator, type JudgeInvitation } from "./auth/judge-session";
import { judgeWorkspacePath, resolveJudgeWorkspace } from "./auth/workspace";
import type { RuntimeConfig } from "./contracts";
import { BrowserPublicationApiClient } from "./publication/api-client";
import { BrowserAccountSetupClient, type AccountWorkspaceConfiguration } from "./auth/account-workspace";
import { LiveStoreConnectionAdapter } from "./store-setup/live-adapter";
import type { AccountUploadBinding } from "./api/client";

/** Keep one in-memory session while selecting the authenticated workspace. */
export function mountApplication(root: Root, config: RuntimeConfig, invitation: JudgeInvitation = captureJudgeInvitation()): void {
  const auth = config.judge_access?.session_entry === true
    ? new JudgeSessionCoordinator(invitation)
    : new OAuthCoordinator(config, undefined, undefined, undefined, undefined, {
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
    const accountConfig: AccountWorkspaceConfiguration | undefined = runtime.judge_access === undefined ? {
      clientId: runtime.client_id,
      cognitoTokenUrl: runtime.cognito_token_url,
      selfServiceSignup: runtime.account_access?.self_service_signup === true,
      ...(runtime.account_access?.issuer === undefined ? {} : { issuer: runtime.account_access.issuer }),
      connectionMethod: runtime.account_access?.connection_method ?? "unavailable",
      connectedWorkflow: runtime.account_access?.connected_workflow === true,
      ...(runtime.account_access?.support_email === undefined ? {} : { supportEmail: runtime.account_access.support_email }),
      ...(runtime.account_access?.notices_version === undefined ? {} : { noticeVersion: runtime.account_access.notices_version }),
    } : undefined;
    const accountDependencies = accountConfig === undefined ? {} : {
      accountConfig, accountApi: new BrowserAccountSetupClient(auth.session, accountConfig),
      ...(accountConfig.connectionMethod !== "personal_token" ? {} : { storeConnectionAdapter: new LiveStoreConnectionAdapter(auth.session, accountConfig) }),
      createAccountWorkflowApi: (binding: AccountUploadBinding) => new BrowserApiClient(auth.session, undefined, binding),
    };
    const judgeAccess = runtime.judge_access === undefined ? undefined : {
      ...(runtime.judge_access.prepared_job_id === undefined ? {} : { preparedJobId: runtime.judge_access.prepared_job_id }),
      ...(runtime.judge_access.cleanup_after_minutes === undefined ? {} : { cleanupAfterMinutes: runtime.judge_access.cleanup_after_minutes }),
    };
    root.render(<StrictMode><App key={judgeAccess === undefined ? "seller" : "judge"} dependencies={{ auth, api, publicationApi, ...accountDependencies, ...(judgeAccess === undefined ? {} : { judgeAccess }) }} /></StrictMode>);
  }
  renderApplication(config);
  if (auth instanceof JudgeSessionCoordinator && !["/judge/privacy", "/judge/terms"].includes(window.location.pathname)) void auth.restore();
}
