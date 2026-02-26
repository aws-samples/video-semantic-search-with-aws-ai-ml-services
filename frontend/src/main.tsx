// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from "react";
import ReactDOM from "react-dom/client";

import "@cloudscape-design/global-styles/index.css";

import App from "./app";
import {
  createBrowserRouter,
  RouterProvider,
  Navigate,
} from "react-router-dom";
import Search from "./pages/search";
import Admin from "./pages/admin";
import AdminDetail from "./pages/admin-detail";
import { Authenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import { Auth } from "aws-amplify";

/**
 * RedirectByRole checks the current user's Cognito groups and redirects
 * admin users to /admin and all other users to /search.
 */
const RedirectByRole: React.FC = () => {
  const [target, setTarget] = React.useState<string | null>(null);

  React.useEffect(() => {
    const checkRole = async () => {
      try {
        const session = await Auth.currentSession();
        const groups: string[] =
          session.getIdToken().payload["cognito:groups"] || [];
        if (groups.includes("admin")) {
          setTarget("/admin");
        } else {
          setTarget("/search");
        }
      } catch {
        setTarget("/search");
      }
    };
    checkRole();
  }, []);

  if (target === null) {
    return null;
  }

  return <Navigate to={target} replace />;
};

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <RedirectByRole /> },
      { path: "admin", element: <Admin /> },
      { path: "admin/:jobId", element: <AdminDetail /> },
      { path: "search", element: <Search /> },
    ],
  },
]);

const root = ReactDOM.createRoot(document.getElementById("root")!);

root.render(
  <Authenticator loginMechanisms={["username"]}>
    {() => <RouterProvider router={router} />}
  </Authenticator>
);
