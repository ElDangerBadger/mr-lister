import { z } from "zod";
import type { RuntimeConfig } from "../contracts";
import { loadRuntimeConfig } from "../runtime";
import { validateReturnPath } from "./session";

const routingClaims = z.object({
  token_use: z.literal("access"),
  client_id: z.string(),
  iss: z.string(),
  "cognito:groups": z.array(z.string().max(256)).max(100),
});

/** Presentation only. API authorization still validates the token and job owner. */
export function isJudgeSignIn(accessToken: string, config: RuntimeConfig): boolean {
  if (accessToken.length > 16_384) return false;
  const parts = accessToken.split(".");
  if (parts.length !== 3 || !parts[1] || !/^[A-Za-z0-9_-]+$/u.test(parts[1])) return false;
  try {
    const encoded = parts[1].replace(/-/gu, "+").replace(/_/gu, "/");
    const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
    const claims = routingClaims.safeParse(JSON.parse(new TextDecoder().decode(bytes)) as unknown);
    if (!claims.success || claims.data.client_id !== config.client_id) return false;
    const issuer = /^https:\/\/cognito-idp\.([a-z0-9-]+)\.amazonaws\.com\/([a-z0-9-]+_[A-Za-z0-9]+)$/u.exec(claims.data.iss);
    if (issuer === null || !new URL(config.cognito_token_url).hostname.endsWith(`.auth.${issuer[1]}.amazoncognito.com`)) return false;
    return claims.data["cognito:groups"].includes(`${issuer[2]}_MrListerJudge`);
  } catch {
    return false;
  }
}

export async function resolveJudgeWorkspace(
  accessToken: string,
  current: RuntimeConfig,
  fetcher: typeof fetch = window.fetch.bind(window),
): Promise<RuntimeConfig | null> {
  if (current.judge_access !== undefined || !isJudgeSignIn(accessToken, current)) return null;
  const next = await loadRuntimeConfig(fetcher, "/judge");
  // Reuse the existing memory session only within this same OAuth authority/client.
  const fields = ["client_id", "cognito_authorize_url", "cognito_token_url", "cognito_logout_url"] as const;
  if (fields.some((field) => next[field] !== current[field]) || next.scopes.join(" ") !== current.scopes.join(" ")) {
    throw new Error("Judge workspace does not match the sign-in authority.");
  }
  return next;
}

export function judgeWorkspacePath(returnPath: string): string {
  const safePath = validateReturnPath(returnPath);
  return safePath === "/" ? "/judge" : `/judge${safePath}`;
}
