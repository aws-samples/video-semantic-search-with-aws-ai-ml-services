import React from "react";
import BreadcrumbGroup, {
  BreadcrumbGroupProps,
} from "@cloudscape-design/components/breadcrumb-group";

const items: BreadcrumbGroupProps.Item[] = [
  { text: "AWS Video Semantic Search", href: "/" },
];

export interface BreadcrumbsProps {
  active: BreadcrumbGroupProps.Item;
}

export default function Breadcrumbs({ active }: BreadcrumbsProps) {
  return <BreadcrumbGroup items={[...items, active]} />;
}
