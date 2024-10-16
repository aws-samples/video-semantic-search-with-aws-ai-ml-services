// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from "react";
import ReactDOM from "react-dom/client";

import "@cloudscape-design/global-styles/index.css";

import App from "./app";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import Home from "./pages/home";

const root = ReactDOM.createRoot(document.getElementById("root")!);

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [{ index: true, element: <Home /> }],
  },
]);

root.render(
  <>
    <RouterProvider router={router} />
  </>
);
