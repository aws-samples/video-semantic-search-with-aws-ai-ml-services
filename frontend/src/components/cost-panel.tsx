// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Box from "@cloudscape-design/components/box";

export interface CostEntry {
  service: string;
  operation: string;
  costUsd: string;
}

interface CostPanelProps {
  entries: CostEntry[];
  totalCostUsd: number;
}

const CostPanel: React.FC<CostPanelProps> = ({ entries, totalCostUsd }) => {
  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Breakdown of AWS service costs for this indexing job"
        >
          Cost Breakdown
        </Header>
      }
    >
      <Table
        columnDefinitions={[
          {
            id: "service",
            header: "Service",
            cell: (item) => item.service,
            sortingField: "service",
          },
          {
            id: "operation",
            header: "Operation",
            cell: (item) => item.operation,
          },
          {
            id: "costUsd",
            header: "Cost (USD)",
            cell: (item) => `$${item.costUsd}`,
          },
        ]}
        items={entries}
        variant="embedded"
        empty={
          <Box textAlign="center" color="inherit">
            <b>No cost data available</b>
            <Box padding={{ bottom: "s" }} variant="p" color="inherit">
              Cost information will appear after the job completes.
            </Box>
          </Box>
        }
        footer={
          <Box textAlign="right" fontWeight="bold">
            Total: ${totalCostUsd.toFixed(4)}
          </Box>
        }
      />
    </Container>
  );
};

export default CostPanel;
