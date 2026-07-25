import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { AuthProvider, AuthGate } from "./auth";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <AuthGate>
        <App />
      </AuthGate>
    </AuthProvider>
  </React.StrictMode>
);
