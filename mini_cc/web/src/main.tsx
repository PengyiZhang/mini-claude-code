import React from "react";
import ReactDOM from "react-dom/client";
import { createHashRouter, RouterProvider } from "react-router-dom";
import App from "./App";
import Login from "./pages/Login";
import Tenants from "./pages/Tenants";
import Projects from "./pages/Projects";
import Workspace from "./pages/Workspace";
import "./index.css";

const router = createHashRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Login /> },
      { path: "tenants", element: <Tenants /> },
      { path: "projects", element: <Projects /> },
      { path: "projects/:pid", element: <Workspace /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
