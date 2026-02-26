// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from "react";
import Navigation from "./components/navigation";
import HelpPanel from "@cloudscape-design/components/help-panel";
import { Amplify, Auth } from "aws-amplify";
import { useAuthenticator } from "@aws-amplify/ui-react";
import "./styles.css";
import { AppLayout, TopNavigation } from "@cloudscape-design/components";
import { Outlet, useNavigate } from "react-router-dom";

import {
  AWS_API_URL,
  AWS_REGION,
  AWS_USER_POOL_ID,
  AWS_USER_POOL_WEB_CLIENT_ID,
} from "./constants";

Amplify.configure({
  oauth: {},
  aws_cognito_username_attributes: [],
  aws_cognito_social_providers: [],
  aws_cognito_signup_attributes: [],
  aws_cognito_mfa_configuration: "OFF",
  aws_cognito_mfa_types: ["SMS"],
  aws_cognito_password_protection_settings: {
    passwordPolicyMinLength: 8,
    passwordPolicyCharacters: [],
  },
  aws_cognito_verification_mechanisms: [],
  aws_appsync_authenticationType: "AMAZON_COGNITO_USER_POOLS",
  aws_project_region: AWS_REGION,
  aws_cognito_region: AWS_REGION,
  aws_user_pools_id: AWS_USER_POOL_ID,
  aws_user_pools_web_client_id: AWS_USER_POOL_WEB_CLIENT_ID,
});

const App = () => {
  const navigate = useNavigate();
  const { signOut, user } = useAuthenticator((context) => [context.user]);

  const [groups, setGroups] = React.useState<string[]>([]);

  React.useEffect(() => {
    const fetchGroups = async () => {
      try {
        const session = await Auth.currentSession();
        const g: string[] =
          session.getIdToken().payload["cognito:groups"] || [];
        setGroups(g);
      } catch {
        setGroups([]);
      }
    };
    fetchGroups();
  }, [user]);

  const isAdmin = groups.includes("admin");

  const utilities: any[] = [];

  if (isAdmin) {
    utilities.push({
      type: "button",
      text: "Admin",
      onClick: () => navigate("/admin"),
    });
  }

  utilities.push({
    type: "button",
    text: "Search",
    iconName: "search",
    onClick: () => navigate("/search"),
  });

  utilities.push({
    type: "menu-dropdown",
    description: user?.attributes?.email,
    iconName: "user-profile",
    onItemClick: ({ detail }: { detail: { id: string } }) => {
      if (detail.id === "signout" && signOut) {
        signOut();
      }
    },
    items: [{ id: "signout", text: "Sign out" }],
  });

  return (
    <>
      <div id="top-nav">
        <TopNavigation
          identity={{
            logo: { src: "/logo.svg", alt: "AWS Video Semantic Search" },
            title: "Video Semantic Search",
            href: "/",
          }}
          i18nStrings={{
            overflowMenuTriggerText: "More",
            overflowMenuTitleText: "All",
          }}
          utilities={utilities}
        />
      </div>
      <AppLayout
        contentType="default"
        maxContentWidth={Number.MAX_VALUE}
        navigationHide={true}
        navigation={<Navigation />}
        tools={<HelpPanel header={<h2>Help panel</h2>} />}
        stickyNotifications={true}
        content={<Outlet />}
        headerSelector="#top-nav"
        toolsHide={true}
        ariaLabels={{
          navigation: "Navigation drawer",
          navigationClose: "Close navigation drawer",
          navigationToggle: "Open navigation drawer",
          notifications: "Notifications",
          tools: "Help panel",
          toolsClose: "Close help panel",
          toolsToggle: "Open help panel",
        }}
      />
    </>
  );
};

export default App;
