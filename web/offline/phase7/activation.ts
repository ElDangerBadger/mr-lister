export const frozenPhase7BrowserActivation = Object.freeze({
  contract_version: "7.0.1",
  request_enabled: false,
  query_enabled: false,
  publication_enabled: false,
  scaffold_only: true,
} as const);

export class Phase7BrowserDisabledError extends Error {
  constructor() {
    super("Phase 7 seller publication is disabled");
    this.name = "Phase7BrowserDisabledError";
  }
}

/**
 * Preserve the future browser composition seam without constructing a client or
 * observing seller authority under the frozen 7.0.1 activation tuple.
 */
export function buildDisabledPublicationBrowser<T>(_build: () => T): never {
  void _build;
  throw new Phase7BrowserDisabledError();
}
