export type ThemePreference = "light" | "dark" | "auto";

export const THEME_STORAGE_KEY = "mr-lister-display-theme";

export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // A display preference must not prevent use of the app when storage is blocked.
  }
  return "auto";
}

export function applyThemePreference(preference: ThemePreference): void {
  const dark = preference === "dark"
    || (preference === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

export function initializeTheme(): void {
  applyThemePreference(readThemePreference());
}
