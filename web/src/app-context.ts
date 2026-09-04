import { createContext, useContext } from "react";
import type { ApiPort } from "./api/client";
import type { AuthCoordinator } from "./auth/session";
import type { PublicationApiPort } from "./publication/api-client";

export interface AppDependencies {
  api: ApiPort;
  auth: AuthCoordinator;
  publicationApi?: PublicationApiPort;
}

export const AppContext = createContext<AppDependencies | null>(null);

export function useAppDependencies(): AppDependencies {
  const dependencies = useContext(AppContext);
  if (dependencies === null) throw new Error("Application dependencies are missing");
  return dependencies;
}
