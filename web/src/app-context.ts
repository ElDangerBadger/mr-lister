import { createContext, useContext } from "react";
import type { AccountUploadBinding, ApiPort } from "./api/client";
import type { AuthCoordinator } from "./auth/session";
import type { PublicationApiPort } from "./publication/api-client";
import type { AccountSetupPort, AccountWorkspaceConfiguration } from "./auth/account-workspace";
import type { StoreConnectionAdapter } from "./store-setup/connection-adapter";

export interface AppDependencies {
  api: ApiPort;
  auth: AuthCoordinator;
  publicationApi?: PublicationApiPort;
  accountConfig?: AccountWorkspaceConfiguration;
  accountApi?: AccountSetupPort;
  storeConnectionAdapter?: StoreConnectionAdapter;
  createAccountWorkflowApi?: (binding: AccountUploadBinding) => ApiPort;
  /** Public presentation settings only; every API request still uses the authenticated owner. */
  judgeAccess?: { preparedJobId?: string; cleanupAfterMinutes?: 30 };
}

export const AppContext = createContext<AppDependencies | null>(null);

export function useAppDependencies(): AppDependencies {
  const dependencies = useContext(AppContext);
  if (dependencies === null) throw new Error("Application dependencies are missing");
  return dependencies;
}
