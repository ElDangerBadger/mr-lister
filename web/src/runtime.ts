import { runtimeConfigSchemaForOrigin, type RuntimeConfig } from "./contracts";

export async function loadRuntimeConfig(fetcher: typeof fetch = window.fetch.bind(window)): Promise<RuntimeConfig> {
  const response = await fetcher("/runtime-config.json", {
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
  const parsed = runtimeConfigSchemaForOrigin(window.location.origin).safeParse(candidate);
  if (!parsed.success) throw new Error("Seller application configuration is invalid.");
  return parsed.data;
}
