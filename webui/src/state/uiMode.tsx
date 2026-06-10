// Guided vs Advanced UI mode, persisted to localStorage. Guided is the
// step-by-step wizard for newcomers; Advanced is the full console. When no
// preference is stored yet, App picks the default from whether any
// workspaces exist (fresh install -> guided, returning user -> advanced).
import { createContext, useCallback, useContext, useMemo, useState } from "react";

export type UiMode = "guided" | "advanced";

interface Ctx {
  mode: UiMode | null; // null = no stored preference yet
  setMode: (mode: UiMode) => void;
}

const UiModeContext = createContext<Ctx>({ mode: null, setMode: () => {} });
const STORAGE_KEY = "clk.uiMode";

export function UiModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<UiMode | null>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return stored === "guided" || stored === "advanced" ? stored : null;
    } catch {
      return null;
    }
  });

  const setMode = useCallback((m: UiMode) => {
    setModeState(m);
    try {
      localStorage.setItem(STORAGE_KEY, m);
    } catch {
      /* ignore */
    }
  }, []);

  const value = useMemo(() => ({ mode, setMode }), [mode, setMode]);
  return <UiModeContext.Provider value={value}>{children}</UiModeContext.Provider>;
}

export function useUiMode(): Ctx {
  return useContext(UiModeContext);
}
