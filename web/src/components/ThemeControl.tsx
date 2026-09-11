import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { applyThemePreference, readThemePreference, THEME_STORAGE_KEY, type ThemePreference } from "../theme";

const options = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "auto", label: "Auto" },
] as const;

export function ThemeControl() {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const optionButtons = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();
  const selectedIndex = options.findIndex((option) => option.value === preference);
  const selectedLabel = options[selectedIndex]?.label ?? "Auto";

  useLayoutEffect(() => {
    applyThemePreference(preference);
    const system = window.matchMedia("(prefers-color-scheme: dark)");
    const onSystemChange = () => applyThemePreference(preference);
    system.addEventListener("change", onSystemChange);
    return () => system.removeEventListener("change", onSystemChange);
  }, [preference]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY || event.key === null) {
        setPreference(readThemePreference());
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    if (!open) return;
    optionButtons.current[selectedIndex]?.focus();
    const dismissOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismissOutside);
    return () => document.removeEventListener("pointerdown", dismissOutside);
  }, [open, selectedIndex]);

  function select(value: ThemePreference) {
    setPreference(value);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, value);
    } catch {
      // Keep the choice for this page even when browser storage is unavailable.
    }
    setOpen(false);
    trigger.current?.focus();
  }

  return (
    <div className="theme-control" ref={root} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <button
        className="theme-trigger"
        ref={trigger}
        type="button"
        aria-label={`Display theme: ${selectedLabel}`}
        title={`Display theme: ${selectedLabel}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <ThemeIcon value={preference} />
      </button>
      {open && (
        <div
          className="theme-menu"
          role="menu"
          aria-label="Display theme"
          id={menuId}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
              trigger.current?.focus();
            } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
              event.preventDefault();
              const currentIndex = optionButtons.current.findIndex((button) => button === document.activeElement);
              const index = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
                : (currentIndex + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
              optionButtons.current[index]?.focus();
            }
          }}
        >
          {options.map((option, index) => (
            <button
              className="theme-option"
              ref={(button) => { optionButtons.current[index] = button; }}
              type="button"
              role="menuitemradio"
              aria-checked={preference === option.value}
              tabIndex={-1}
              key={option.value}
              onClick={() => select(option.value)}
            >
              <ThemeIcon value={option.value} />
              <span>{option.label}</span>
              {preference === option.value && <svg className="theme-check" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m5 12 4 4L19 6" /></svg>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ThemeIcon({ value }: { value: ThemePreference }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      {value === "light" ? <><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42" /></>
        : value === "dark" ? <path d="M20.8 13A9 9 0 0 1 11 3.2 9 9 0 1 0 20.8 13Z" />
          : <><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8m-4-4v4" /></>}
    </svg>
  );
}
