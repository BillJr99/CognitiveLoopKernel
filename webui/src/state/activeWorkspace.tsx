// Holds the single "active workspace" (TUI-like: one project in focus at
// a time) and persists the choice to localStorage so a refresh keeps it.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

interface Ctx {
  activeId: string | null;
  setActiveId: (id: string | null) => void;
}

const WorkspaceContext = createContext<Ctx>({ activeId: null, setActiveId: () => {} });
const STORAGE_KEY = "clk.activeWorkspace";

export function ActiveWorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [activeId, setActiveIdState] = useState<string | null>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  });

  const setActiveId = useCallback((id: string | null) => {
    setActiveIdState(id);
    try {
      if (id) localStorage.setItem(STORAGE_KEY, id);
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    /* noop: provider only owns the value */
  }, [activeId]);

  const value = useMemo(() => ({ activeId, setActiveId }), [activeId, setActiveId]);
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useActiveWorkspace(): Ctx {
  return useContext(WorkspaceContext);
}
