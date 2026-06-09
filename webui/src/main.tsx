import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { ActiveWorkspaceProvider } from "./state/activeWorkspace";
import { ActivityStreamProvider } from "./state/activity";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ActiveWorkspaceProvider>
        <ActivityStreamProvider>
          <App />
        </ActivityStreamProvider>
      </ActiveWorkspaceProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
