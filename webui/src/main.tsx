import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/inter";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/jetbrains-mono/400.css";
import App from "./App";
import { ActiveWorkspaceProvider } from "./state/activeWorkspace";
import { ActivityStreamProvider } from "./state/activity";
import { UiModeProvider } from "./state/uiMode";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ActiveWorkspaceProvider>
        <UiModeProvider>
          <ActivityStreamProvider>
            <App />
          </ActivityStreamProvider>
        </UiModeProvider>
      </ActiveWorkspaceProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
