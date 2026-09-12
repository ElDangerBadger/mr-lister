import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemeControl } from "../src/components/ThemeControl";
import { initializeTheme, readThemePreference } from "../src/theme";

const displayKey = "mr-lister-display-theme";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("display preference storage boundary", () => {
  it("persists only the display key and one of the three choices", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("unrelated-preference", "leave-alone");
    const writes = vi.spyOn(Storage.prototype, "setItem");
    render(<ThemeControl />);
    expect(writes).not.toHaveBeenCalled();

    for (const label of ["Light", "Dark", "Auto"]) {
      await user.click(screen.getByRole("button", { name: /^Display theme:/u }));
      await user.click(screen.getByRole("menuitemradio", { name: label }));
      expect(screen.getByRole("button", { name: `Display theme: ${label}` })).toBeInTheDocument();
      expect(document.documentElement.dataset.theme).toBe(label === "Dark" ? "dark" : "light");
    }

    expect(writes.mock.calls).toEqual([
      [displayKey, "light"], [displayKey, "dark"], [displayKey, "auto"],
    ]);
    expect(window.localStorage.length).toBe(2);
    expect(window.localStorage.getItem("unrelated-preference")).toBe("leave-alone");
  });

  it("accepts only display values on reload and falls back to Auto for unknown stored data", () => {
    for (const [stored, expected] of [
      ["light", "light"], ["dark", "dark"], ["auto", "auto"],
      ["unexpected", "auto"], ['{"access_token":"must-not-be-used"}', "auto"],
    ] as const) {
      window.localStorage.setItem(displayKey, stored);
      expect(readThemePreference()).toBe(expected);
      initializeTheme();
      expect(document.documentElement.dataset.theme).toBe(expected === "dark" ? "dark" : "light");
    }
    window.localStorage.removeItem(displayKey);
    expect(readThemePreference()).toBe("auto");
  });

  it("keeps the display control usable when storage reads and writes are blocked", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    expect(readThemePreference()).toBe("auto");
    render(<ThemeControl />);
    await user.click(screen.getByRole("button", { name: "Display theme: Auto" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Dark" }));
    expect(screen.getByRole("button", { name: "Display theme: Dark" })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
