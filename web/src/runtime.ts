import { runtimeConfigSchemaForOrigin, type RuntimeConfig } from "./contracts";

export async function loadRuntimeConfig(
  fetcher: typeof fetch = window.fetch.bind(window),
  basePath: "" | "/judge" = workspaceBasePath(window.location.pathname),
): Promise<RuntimeConfig> {
  const response = await fetcher(`${basePath}/runtime-config.json`, {
    cache: "no-store",
    credentials: "omit",
    headers: { Accept: "application/json" },
    redirect: "error",
  });
  if (!response.ok) throw new Error("Seller application configuration is unavailable.");
  const text = await response.text();
  if (text.length > 16_384) throw new Error("Seller application configuration is invalid.");
  let candidate: unknown;
  try {
    candidate = JSON.parse(text) as unknown;
  } catch {
    throw new Error("Seller application configuration is invalid.");
  }
  const parsed = runtimeConfigSchemaForOrigin(window.location.origin, basePath).safeParse(candidate);
  if (!parsed.success) throw new Error("Seller application configuration is invalid.");
  return parsed.data;
}

export function workspaceBasePath(pathname: string): "" | "/judge" {
  return pathname === "/judge" || pathname.startsWith("/judge/") ? "/judge" : "";
}

/** Finish the second half of sign-out after the primary Cognito cookie is cleared. */
export function judgeSignOutTarget(config: RuntimeConfig, pathname: string): URL | null {
  if (pathname !== "/judge/signout" || config.judge_access === undefined) return null;
  const target = new URL(config.judge_access.upstream_logout_url);
  target.searchParams.set("client_id", config.judge_access.upstream_client_id);
  target.searchParams.set("logout_uri", new URL("/judge/", config.redirect_uri).href);
  return target;
}
