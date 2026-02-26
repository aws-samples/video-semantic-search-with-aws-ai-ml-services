// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useRef, useState, useCallback } from "react";
import Container from "@cloudscape-design/components/container";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Table from "@cloudscape-design/components/table";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Alert from "@cloudscape-design/components/alert";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import TextFilter from "@cloudscape-design/components/text-filter";
import { useAuthenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import axios from "axios";
import { Auth } from "aws-amplify";
import { useNavigate } from "react-router-dom";
import { AWS_API_URL } from "../constants";
import IndexingConfigModal, {
  IndexingConfig,
} from "../components/indexing-config-modal";

const getAuthToken = async () => {
  try {
    const session = await Auth.currentSession();
    return session.getIdToken().getJwtToken();
  } catch (error) {
    console.error("Error getting auth token:", error);
    return null;
  }
};

const authenticatedAxios = axios.create();
authenticatedAxios.interceptors.request.use(
  async (config) => {
    const token = await getAuthToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  },
);

interface TableData {
  jobId: string;
  jobStatus: string;
  startTime: string;
  endTime: string;
  jobInput: string;
}

function getStatusType(
  status: string,
): "success" | "error" | "warning" | "info" | "loading" | "stopped" {
  switch (status.toLowerCase()) {
    case "completed":
    case "succeeded":
      return "success";
    case "failed":
    case "error":
      return "error";
    case "running":
    case "processing":
    case "in_progress":
      return "loading";
    case "pending":
    case "queued":
      return "info";
    case "deleting":
      return "loading";
    case "cancelled":
    case "stopped":
      return "stopped";
    default:
      return "info";
  }
}

const Admin: React.FC = () => {
  const { user } = useAuthenticator((context) => [context.user]);
  const userId = user?.username;
  const navigate = useNavigate();

  // Table state
  const [tableData, setTableData] = useState<TableData[]>([]);
  const [isTableLoading, setIsTableLoading] = useState(false);
  const [filterText, setFilterText] = useState("");

  // Upload state
  const [isUploadDisabled, setIsUploadDisabled] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressInfo, setProgressInfo] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Modal state
  const [isConfigModalVisible, setIsConfigModalVisible] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<FileList | null>(null);

  // Duplicate rename state: original name → new name
  const [renameMap, setRenameMap] = useState<Record<string, string>>({});
  const [renameWarningVisible, setRenameWarningVisible] = useState(false);

  // Delete confirmation state
  const [deleteTarget, setDeleteTarget] = useState<TableData | null>(null);

  // Hidden file input ref
  const fileInputRef = useRef<HTMLInputElement>(null);

  const addItem = useCallback((item: TableData) => {
    setTableData((prev) => [item, ...prev]);
  }, []);

  // Load all jobs on mount
  useEffect(() => {
    if (userId) {
      loadAllJobs();
    }
  }, [userId]);

  const loadAllJobs = async () => {
    setIsTableLoading(true);
    try {
      const response = await authenticatedAxios.get(
        AWS_API_URL + "/get_all_jobs",
      );
      if (response.status === 200) {
        let jobs = response.data;
        jobs = jobs
          .slice()
          .sort((jobA: { Started: string }, jobB: { Started: string }) => {
            return (
              new Date(jobB.Started).getTime() -
              new Date(jobA.Started).getTime()
            );
          });

        const items: TableData[] = jobs.map(
          (job: {
            JobId: string;
            Status: string;
            Started: string;
            EndTime: string;
            Input: string;
          }) => ({
            jobId: job.JobId,
            jobStatus: job.Status,
            startTime: job.Started,
            endTime: job.EndTime === "-" ? "" : job.EndTime,
            jobInput: job.Input,
          }),
        );
        setTableData(items);
      }
    } catch (error) {
      console.error("Error loading jobs:", error);
      setErrorMessage("Failed to load jobs. Please try again.");
    } finally {
      setIsTableLoading(false);
    }
  };

  // Trigger hidden file input
  const handleUploadClick = () => {
    if (fileInputRef.current && !isUploadDisabled) {
      fileInputRef.current.click();
      fileInputRef.current.value = "";
    }
  };

  // Compute a unique filename by appending (1), (2), etc.
  const getUniqueName = (name: string): string => {
    const existingNames = new Set(tableData.map((job) => job.jobInput));
    if (!existingNames.has(name)) return name;

    const dotIdx = name.lastIndexOf(".");
    const base = dotIdx > 0 ? name.slice(0, dotIdx) : name;
    const ext = dotIdx > 0 ? name.slice(dotIdx) : "";
    let counter = 1;
    while (existingNames.has(`${base} (${counter})${ext}`)) {
      counter++;
    }
    return `${base} (${counter})${ext}`;
  };

  // File selected - check for duplicates, then open config modal
  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) return;

    const renames: Record<string, string> = {};
    let hasDuplicates = false;
    for (let i = 0; i < files.length; i++) {
      const file = files.item(i);
      if (!file) continue;
      const uniqueName = getUniqueName(file.name);
      if (uniqueName !== file.name) {
        renames[file.name] = uniqueName;
        hasDuplicates = true;
      }
    }

    setPendingFiles(files);
    setRenameMap(renames);
    if (hasDuplicates) {
      setRenameWarningVisible(true);
    } else {
      setIsConfigModalVisible(true);
    }
  };

  // Config confirmed - start upload and indexing
  const handleConfigConfirm = (config: IndexingConfig) => {
    setIsConfigModalVisible(false);
    if (userId && pendingFiles && pendingFiles.length > 0) {
      setIsUploadDisabled(true);
      uploadVideoAndCreateJobs(userId, pendingFiles, config);
    }
    setPendingFiles(null);
  };

  const handleConfigDismiss = () => {
    setIsConfigModalVisible(false);
    setPendingFiles(null);
  };

  // Rename warning confirmed - proceed to config modal
  const handleRenameConfirm = () => {
    setRenameWarningVisible(false);
    setIsConfigModalVisible(true);
  };

  const handleRenameDismiss = () => {
    setRenameWarningVisible(false);
    setRenameMap({});
    setPendingFiles(null);
  };

  // Upload videos and create indexing jobs
  const uploadVideoAndCreateJobs = async (
    userId: string,
    videoFiles: FileList,
    config: IndexingConfig,
  ) => {
    setProgress(0);
    setProgressInfo("Uploading video...");
    setErrorMessage(null);

    const allowedFilenameRegex = /^[a-zA-Z0-9._ ()-]+\.(mp4)$/;
    let uploadCount = 0;
    const totalProgress: number[] = new Array(videoFiles.length).fill(0);

    for (let index = 0; index < videoFiles.length; index++) {
      const videoFile = videoFiles.item(index);
      if (!videoFile || !allowedFilenameRegex.test(videoFile.name)) {
        setProgressInfo("No input file or invalid input filename");
        setIsUploadDisabled(false);
        return;
      }

      const uploadName = renameMap[videoFile.name] || videoFile.name;

      try {
        // Get presigned URL (use renamed filename if duplicate)
        const presignedResponse = await authenticatedAxios.get(
          AWS_API_URL +
            "/presignedurl_video?type=post&object_name=" +
            encodeURIComponent(uploadName),
        );

        if (presignedResponse.status === 200) {
          const presignedUrl = presignedResponse.data.url;
          const fields = presignedResponse.data.fields;

          const formData = new FormData();
          formData.append("key", fields["key"]);
          formData.append("AWSAccessKeyId", fields["AWSAccessKeyId"]);
          formData.append(
            "x-amz-security-token",
            fields["x-amz-security-token"],
          );
          formData.append("policy", fields["policy"]);
          formData.append("signature", fields["signature"]);
          formData.append("file", videoFile);

          // Upload to S3
          const uploadResponse = await axios.post(presignedUrl, formData, {
            onUploadProgress: (progressEvent) => {
              if (!progressEvent.total) return;
              totalProgress[index] =
                (progressEvent.loaded / progressEvent.total) * 100;
              const overallProgress =
                Object.values(totalProgress).reduce(
                  (acc, value) => acc + value,
                  0,
                ) / videoFiles.length;
              setProgress(overallProgress);
            },
          });

          if (uploadResponse.status === 204) {
            uploadCount++;
            if (uploadCount === videoFiles.length) {
              // All uploads complete - create indexing jobs
              await createIndexingJobs(userId, videoFiles, config);
            }
          }
        }
      } catch (error) {
        console.error("Upload error:", error);
        setErrorMessage("Upload failed. Please try again.");
        setIsUploadDisabled(false);
      }
    }
  };

  const createIndexingJobs = async (
    userId: string,
    videoFiles: FileList,
    config: IndexingConfig,
  ) => {
    const percentage = Math.floor(Math.random() * (90 - 70 + 1)) + 70;
    setProgress(percentage);
    setProgressInfo("Creating indexing job...");

    let count = 0;
    for (const videoFile of videoFiles) {
      const uploadName = renameMap[videoFile.name] || videoFile.name;
      try {
        const response = await authenticatedAxios.get(
          AWS_API_URL +
            "/create_job?userId=" +
            userId +
            "&video_name=" +
            encodeURIComponent(uploadName),
        );

        if (response.status === 200) {
          count++;
          setProgress(
            Math.floor(Math.random() * (99 - percentage + 1)) + percentage,
          );

          if (count === videoFiles.length) {
            setProgress(100);
            setProgressInfo("Indexing job is successfully created.");
            setIsUploadDisabled(false);
          }

          const item: TableData = {
            jobId: response.data["jobId"],
            jobStatus: response.data["status"],
            startTime: response.data["started"],
            endTime: "",
            jobInput: response.data["input"],
          };
          addItem(item);
        }
      } catch (error) {
        console.error("Create job error:", error);
        setErrorMessage(
          "It seems there was an error processing your request. Please try again!",
        );
        setIsUploadDisabled(false);
      }
    }
  };

  // Delete a job — immediately set status to "Deleting" and fire the API call
  // in the background. The Lambda marks DynamoDB as "Deleting" early, so even
  // if API Gateway times out (30s), the next refresh shows correct status.
  const handleDeleteJob = () => {
    if (!deleteTarget) return;
    const jobId = deleteTarget.jobId;

    // Immediately update the table to show "Deleting" status
    setTableData((prev) =>
      prev.map((item) =>
        item.jobId === jobId ? { ...item, jobStatus: "Deleting" } : item,
      ),
    );
    setDeleteTarget(null);

    // Fire the delete request in the background
    authenticatedAxios
      .delete(AWS_API_URL + "/admin/jobs/" + jobId)
      .then((response) => {
        if (response.status === 200 || response.status === 207) {
          setTableData((prev) => prev.filter((item) => item.jobId !== jobId));
          if (response.status === 207 && response.data?.warnings) {
            setErrorMessage(
              "Job deleted with warnings: " + response.data.warnings.join("; "),
            );
          }
        }
      })
      .catch((error) => {
        console.error("Delete job error:", error);
        // Don't revert status — the Lambda is still running in the background.
        // The job will be removed on the next refresh once deletion completes.
      });
  };

  return (
    <>
      {/* Hidden file input */}
      <div style={{ display: "none" }}>
        <input
          type="file"
          ref={fileInputRef}
          onChange={handleFileChange}
          multiple
          accept=".mp4"
        />
      </div>

      {/* Indexing config modal */}
      <IndexingConfigModal
        visible={isConfigModalVisible}
        onDismiss={handleConfigDismiss}
        onConfirm={handleConfigConfirm}
      />

      {/* Duplicate video name rename modal */}
      <Modal
        visible={renameWarningVisible}
        onDismiss={handleRenameDismiss}
        header="Duplicate video name"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleRenameDismiss}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleRenameConfirm}>
                Continue
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        The following file(s) will be renamed to avoid conflicts:
        <ul>
          {Object.entries(renameMap).map(([original, renamed]) => (
            <li key={original}>
              <b>{original}</b> &rarr; <b>{renamed}</b>
            </li>
          ))}
        </ul>
      </Modal>

      {/* Delete confirmation modal */}
      <Modal
        visible={deleteTarget !== null}
        onDismiss={() => setDeleteTarget(null)}
        header="Delete job"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDeleteJob}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Are you sure you want to delete <b>{deleteTarget?.jobInput}</b>? This
        will permanently remove the video, all indexed data, and associated
        resources.
      </Modal>

      <SpaceBetween size="l">
        {errorMessage && (
          <Alert
            type="error"
            dismissible
            onDismiss={() => setErrorMessage(null)}
          >
            {errorMessage}
          </Alert>
        )}

        {isUploadDisabled && (
          <Container>
            <ProgressBar
              value={progress}
              additionalInfo={progressInfo}
              label="Uploading video"
            />
          </Container>
        )}

        <Table
          header={
            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    iconName="refresh"
                    onClick={loadAllJobs}
                    loading={isTableLoading}
                  ></Button>
                  <Button
                    variant="primary"
                    iconName="upload"
                    onClick={handleUploadClick}
                    disabled={isUploadDisabled}
                  >
                    Upload Video
                  </Button>
                </SpaceBetween>
              }
              counter={`(${tableData.length})`}
            >
              Indexing Jobs
            </Header>
          }
          filter={
            <TextFilter
              filteringPlaceholder="Search"
              filteringText={filterText}
              onChange={({ detail }) => setFilterText(detail.filteringText)}
            />
          }
          columnDefinitions={[
            {
              id: "jobInput",
              header: "Video",
              cell: (e) => {
                const name = e.jobInput;
                const display =
                  name.length <= 35
                    ? name
                    : name.slice(0, 20) + "..." + name.slice(-12);
                return (
                  <Link onFollow={() => navigate(`/admin/${e.jobId}`)}>
                    {display}
                  </Link>
                );
              },
              sortingField: "jobInput",
              minWidth: 200,
            },
            {
              id: "jobStatus",
              header: "Status",
              cell: (e) => (
                <StatusIndicator type={getStatusType(e.jobStatus)}>
                  {e.jobStatus}
                </StatusIndicator>
              ),
              sortingField: "jobStatus",
              minWidth: 120,
            },
            {
              id: "startTime",
              header: "Started",
              cell: (e) => e.startTime,
              sortingField: "startTime",
              minWidth: 180,
            },
            {
              id: "endTime",
              header: "End Time",
              cell: (e) => e.endTime || "-",
              sortingField: "endTime",
              minWidth: 180,
            },
            {
              id: "actions",
              header: "",
              cell: (e) =>
                e.jobStatus.toLowerCase() === "deleting" ? null : (
                  <Button
                    iconName="remove"
                    variant="icon"
                    onClick={(evt) => {
                      evt.stopPropagation();
                      setDeleteTarget(e);
                    }}
                  />
                ),
              minWidth: 50,
              maxWidth: 50,
            },
          ]}
          items={
            filterText
              ? tableData.filter((item) =>
                  item.jobInput
                    .toLowerCase()
                    .includes(filterText.toLowerCase()),
                )
              : tableData
          }
          loading={isTableLoading}
          loadingText="Loading jobs..."
          trackBy="jobId"
          empty={
            filterText ? (
              <Box textAlign="center" color="inherit">
                <b>No matching videos</b>
                <Box padding={{ bottom: "s" }} variant="p" color="inherit">
                  No videos match the search filter.
                </Box>
                <Button onClick={() => setFilterText("")}>Clear filter</Button>
              </Box>
            ) : (
              <Box textAlign="center" color="inherit">
                <b>No indexing jobs</b>
                <Box padding={{ bottom: "s" }} variant="p" color="inherit">
                  Upload a video to create your first indexing job.
                </Box>
                <Button
                  variant="primary"
                  iconName="upload"
                  onClick={handleUploadClick}
                >
                  Upload Video
                </Button>
              </Box>
            )
          }
        />
      </SpaceBetween>
    </>
  );
};

export default Admin;
