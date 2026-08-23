import { describe, expect, it, vi } from "vitest";
import { loadRuntimeConfig } from "../src/runtime";

describe("runtime configuration transport", () => {
  it("loads the public no-secret config without cookies or redirects", async () => {
    const config = {
      cognito_authorize_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/authorize",
      cognito_token_url: "https://seller.auth.us-west-2.amazoncognito.com/oauth2/token",
      cognito_logout_url: "https://seller.auth.us-west-2.amazoncognito.com/logout",
      client_id: "public-client",
      redirect_uri: `${window.location.origin}/auth/callback`,
      scopes: ["openid", "mr-lister-api/seller"],
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(config), { status: 200 }));
    await expect(loadRuntimeConfig(fetcher)).resolves.toEqual(config);
    expect(fetcher).toHaveBeenCalledWith("/runtime-config.json", {
      cache: "no-store",
      credentials: "omit",
      headers: { Accept: "application/json" },
      redirect: "error",
    });
  });
});
