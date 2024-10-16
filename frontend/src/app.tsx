import React, { useRef, useState } from "react";
import Navigation from "./components/navigation";
import Breadcrumbs from "./components/breadcrumbs";
import HelpPanel from "@cloudscape-design/components/help-panel";
import { Amplify } from "aws-amplify";
import { AmplifyUser, AuthEventData } from "@aws-amplify/ui";
import { Authenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import "./styles.css";
import { AppLayout, TopNavigation } from "@cloudscape-design/components";
import { Outlet, useLocation } from "react-router-dom";
import Home from "./pages/home";

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

const breadcrumbPages: { [key: string]: string } = {
  "/": "Home",
};

const App = ({
  signOut,
  user,
}: {
  signOut: ((data?: AuthEventData | undefined) => void) | undefined;
  user: AmplifyUser | undefined;
}) => {
  const pagePath: string = useLocation()["pathname"];

  const homeRef = useRef<{ triggerUploadVideo: () => void }>(null);
  const handleTriggerUpload = () => {
    if (homeRef.current) {
      homeRef.current.triggerUploadVideo();
    }
  };

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
          utilities={[
            {
              type: "button",
              text: "Upload Video",
              iconName: "upload",
              onClick: handleTriggerUpload,
            },
            {
              type: "menu-dropdown",
              description: user?.attributes?.email,
              iconName: "user-profile",
              onItemClick: signOut,
              items: [{ id: "signout", text: "Sign out" }],
            },
          ]}
        />
      </div>
      <AppLayout
        contentType="form"
        navigationHide={true}
        navigation={<Navigation />}
        tools={<HelpPanel header={<h2>Help panel</h2>} />}
        stickyNotifications={true}
        content={<Home ref={homeRef} />}
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

const UnauthenticatedApp = () => {
  return (
    <Authenticator loginMechanisms={["username"]}>
      {({ signOut, user }) => <App signOut={signOut} user={user} />}
    </Authenticator>
  );
};

export default UnauthenticatedApp;
