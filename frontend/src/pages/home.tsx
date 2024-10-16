import React, {
  useRef,
  useEffect,
  useState,
  useImperativeHandle,
  forwardRef,
} from "react";
import Container from "@cloudscape-design/components/container";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Grid from "@cloudscape-design/components/grid";
import "@aws-amplify/ui-react/styles.css";
import "../styles.css";
import styled from "styled-components";
import Input from "@cloudscape-design/components/input";
import Table from "@cloudscape-design/components/table";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import { useAuthenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import axios from "axios";
import FileUpload from "@cloudscape-design/components/file-upload";
import FormField from "@cloudscape-design/components/form-field";
import { Auth } from "aws-amplify";

import {
  AWS_API_URL,
  AWS_REGION,
  AWS_USER_POOL_ID,
  AWS_USER_POOL_WEB_CLIENT_ID,
} from "../constants";

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
  }
);

interface TableData {
  jobId: string;
  jobStatus: string;
  startTime: string;
  endTime: string;
  jobInput: string;
}

var jobIds: string[] = [];
var jobStatuses: string[] = [];
var startTimes: string[] = [];
var endTimes: string[] = [];
var jobInputs: string[] = [];

const FlyingCircle = styled.div`
  width: 20px;
  height: 20px;
  margin-top: 25%;
  margin-left: 50%;
  border-radius: 50%;
  border: 2px solid #ccc;
  border-top: 2px solid #3498db;
  animation: spin 1s linear infinite;

  @keyframes spin {
    0% {
      transform: rotate(0deg);
    }
    100% {
      transform: rotate(360deg);
    }
  }
`;

var aoss_index = "vss-index";

const Home = forwardRef((props, ref) => {
  // console.log(import.meta.env);
  const { user, signOut } = useAuthenticator((context) => [context.user]);
  const userId = user?.username;

  const [query, setQuery] = React.useState("");

  const [tableData, setTableData] = useState<TableData[]>([]);
  const [isTableLoading, setIsTableLoading] = useState(false);
  const [isRefreshDisabled, setIsRefreshDisabled] = useState(false);
  const [selectedItems, setSelectedItems] = useState<TableData[]>([]);
  const [progress, setProgress] = useState(0);
  const [progressInfo, setProgressInfo] = useState("");
  const addItem = (item: TableData) => {
    setTableData((prevTableData) => [item, ...prevTableData]);
  };

  const uploadvideo = useRef<HTMLInputElement>(null);
  const [isUploadDisabled, setIsUploadDisabled] = useState(false);
  function triggerUploadVideo() {
    if (uploadvideo.current && !isUploadDisabled) {
      uploadvideo.current.click();
      uploadvideo.current.value = "";
    }
  }

  const handleUploadvideo = (event: React.ChangeEvent<HTMLInputElement>) => {
    let videoFiles = event.target.files;
    if (userId && videoFiles && videoFiles.length > 0) {
      setIsUploadDisabled(true);
      uploadVideoAndCreateJobs(
        userId,
        videoFiles,
        setProgress,
        setProgressInfo,
        setIsUploadDisabled,
        addItem
      );
    }
  };

  const [isSearching, setSearching] = useState(false);
  const [image, setImage] = React.useState([]);

  const handleSearchByImage = (files: string | any[]) => {
    const file = files[0];
    const reader = new FileReader();
    reader.onloadend = () => {
      const base64String = reader.result as string;
      searchByImage(base64String, setSearching);
    };

    reader.readAsDataURL(file);
  };

  useEffect(() => {
    if (userId) {
      getAllJobs(addItem);
    }
  }, [userId]);

  useImperativeHandle(ref, () => ({
    triggerUploadVideo,
  }));

  return (
    <>
      <div style={{ display: "none" }}>
        <input
          type="file"
          id="video"
          ref={uploadvideo}
          onChange={handleUploadvideo}
          multiple
          accept=".mp4"
        />
      </div>
      <SpaceBetween size="l">
        <Input
          className="input"
          onKeyDown={(event) => {
            if (event.detail.key === "Enter") {
              search(query, setSearching);
            }
          }}
          onChange={({ detail }) => setQuery(detail.value)}
          value={query}
          placeholder="Search"
          type="search"
        />

        <FormField className="input-image">
          <FileUpload
            onChange={({ detail }) => handleSearchByImage(detail.value)}
            value={image}
            accept=".jpeg, .jpg, .png"
            i18nStrings={{
              uploadButtonText: (e) =>
                e ? "Search by images" : "Search by image",
              dropzoneText: (e) =>
                e ? "Drop files to upload" : "Drop file to upload",
              removeFileAriaLabel: (e) => `Remove file ${e + 1}`,
              limitShowFewer: "Show fewer files",
              limitShowMore: "Show more files",
              errorIconAriaLabel: "Error",
            }}
            constraintText=""
          />
        </FormField>
        <hr></hr>
        <Grid
          gridDefinition={[
            { colspan: { default: 5, xxs: 7 } },
            { colspan: { default: 7, xxs: 5 } },
          ]}
        >
          <div>
            {isSearching && <FlyingCircle />}
            <SpaceBetween size="xxs" id="search">
              {/* Search */}
            </SpaceBetween>
          </div>
          <Container>
            <ProgressBar
              value={progress}
              additionalInfo={progressInfo}
              label="Add new video to database"
            />
            <Table
              className="jobs-table"
              columnDefinitions={[
                {
                  id: "jobInput",
                  header: "Video",
                  cell: (e) => (
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "left",
                      }}
                    >
                      <p>
                        {e.jobInput.length <= 35
                          ? e.jobInput
                          : e.jobInput.slice(0, 20) +
                            "..." +
                            e.jobInput.slice(-12)}
                      </p>
                    </div>
                  ),
                  minWidth: 70,
                },
                {
                  id: "jobStatus",
                  header: "Status",
                  cell: (e) => e.jobStatus,
                },
                {
                  id: "startTime",
                  header: "Started",
                  cell: (e) => e.startTime,
                },
                {
                  id: "endTime",
                  header: "End Time",
                  cell: (e) => e.endTime,
                },
              ]}
              items={tableData}
              variant="embedded"
              loading={isTableLoading}
              loadingText=""
              trackBy="jobId"
            />
          </Container>
        </Grid>
      </SpaceBetween>
    </>
  );
});
export default Home;

function uploadVideoAndCreateJobs(
  userId: string,
  videoFiles: FileList,
  setProgress: {
    (value: React.SetStateAction<number>): void;
    (arg0: number): void;
  },
  setProgressInfo: {
    (value: React.SetStateAction<string>): void;
    (arg0: string): void;
  },
  setIsUploadDisabled: React.Dispatch<React.SetStateAction<boolean>>,
  addItem: (item: TableData) => void
) {
  setProgress(0);
  setProgressInfo("Uploading video...");
  var count = 0;
  var totalProgress: number[] = new Array(videoFiles.length).fill(0);
  const allowedFilenameRegex = /^[a-zA-Z0-9._ -]+\.(mp4)$/;
  function isValidFilename(filename: string): boolean {
    return allowedFilenameRegex.test(filename);
  }
  for (let index = 0; index < videoFiles.length; index++) {
    const videoFile = videoFiles.item(index);
    const fetchData = async () => {
      if (!videoFile || !isValidFilename(videoFile.name)) {
        setProgressInfo("No input file or invalid input filename");
        setIsUploadDisabled(false);
        return;
      }
      const response = await authenticatedAxios
        .get(
          AWS_API_URL +
            "/presignedurl_video?type=post&object_name=" +
            videoFile.name
        )
        .then((response) => {
          if (response.status == 200) {
            var presignedUrl = response.data.url;
            var fields = response.data.fields;
            var key = fields["key"];
            var AWSAccessKeyId = fields["AWSAccessKeyId"];
            var xAmzSecurityToken = fields["x-amz-security-token"];
            var policy = fields["policy"];
            var signature = fields["signature"];

            var formData = new FormData();
            formData.append("key", key);
            formData.append("AWSAccessKeyId", AWSAccessKeyId);
            formData.append("x-amz-security-token", xAmzSecurityToken);
            formData.append("policy", policy);
            formData.append("signature", signature);
            if (videoFile) formData.append("file", videoFile);

            axios
              .post(presignedUrl, formData, {
                onUploadProgress: (progressEvent) => {
                  if (!progressEvent.total) return;
                  totalProgress[index] =
                    (progressEvent.loaded / progressEvent.total) * 100;
                  // Calculate the overall progress
                  const overallProgress =
                    Object.values(totalProgress).reduce(
                      (acc, value) => acc + value,
                      0
                    ) / videoFiles.length;
                  // Update the overall progress bar
                  setProgress(overallProgress);
                },
              })
              .then((response) => {
                if (response.status == 204) {
                  count++;
                  if (count == videoFiles.length) {
                    createProcessingJobs(
                      userId,
                      videoFiles,
                      setProgress,
                      setProgressInfo,
                      setIsUploadDisabled,
                      addItem
                    );
                  }
                }
              })
              .catch((error) => {
                console.error(error);
              });
          }
        })
        .catch((error) => {
          console.error(error);
        });
    };
    fetchData();
  }
}

function createProcessingJobs(
  userId: string,
  videoFiles: FileList,
  setProgress: {
    (value: React.SetStateAction<number>): void;
    (arg0: number): void;
  },
  setProgressInfo: {
    (value: React.SetStateAction<string>): void;
    (arg0: string): void;
  },
  setIsUploadDisabled: React.Dispatch<React.SetStateAction<boolean>>,
  addItem: (item: TableData) => void
) {
  var percentage = Math.floor(Math.random() * (90 - 70 + 1)) + 70;
  setProgress(percentage);
  setProgressInfo("Creating processing job...");
  var count = 0;
  for (const videoFile of videoFiles) {
    const fetchData = async () => {
      const response = await authenticatedAxios
        .get(
          AWS_API_URL +
            "/create_job?userId=" +
            userId +
            "&video_name=" +
            videoFile.name
        )
        .then((response) => {
          if (response.status == 200) {
            count++;
            setProgress(
              Math.floor(Math.random() * (99 - percentage + 1)) + percentage
            );
            if (count == videoFiles.length) {
              setProgress(100);
              setProgressInfo("Processing job is successfully created.");
              setIsUploadDisabled(false);
            }
            jobIds.unshift(response.data["jobId"]);
            jobStatuses.unshift(response.data["status"]);
            startTimes.unshift(response.data["started"]);
            endTimes.unshift("");
            jobInputs.unshift(response.data["input"]);
            const item = {
              jobId: response.data["jobId"],
              jobStatus: response.data["status"],
              startTime: response.data["started"],
              endTime: "",
              jobInput: response.data["input"],
            };
            addItem(item);
          }
        })
        .catch((error) => {
          setProgressInfo(
            "It seems there was an error processing your request. Please try again!"
          );
          setIsUploadDisabled(false);
          console.error(error);
        });
    };
    fetchData();
  }
}

function search(
  query: string,
  setSearching: {
    (value: React.SetStateAction<boolean>): void;
    (arg0: boolean): void;
  }
) {
  console.clear();
  const videoContainer = document.getElementById("search");
  if (!videoContainer) {
    console.error("Video container not found");
    return;
  }
  while (videoContainer.firstChild) {
    videoContainer.removeChild(videoContainer.firstChild);
  }
  setSearching(true);
  const uniqueTimestamps = new Set<string>();
  const fetchData = async () => {
    const response = await authenticatedAxios
      .get(
        AWS_API_URL +
          "/search?type=text&index=" +
          aoss_index +
          "&query=" +
          query
      )
      .then((response) => {
        setSearching(false);
        if (response.status == 200) {
          const results = response.data;
          results.forEach((result: { [x: string]: string }) => {
            let timestamp = parseInt(result["shot_startTime"]);
            let key = result["video_name"] + timestamp;
            if (!uniqueTimestamps.has(key)) {
              const videoElement = document.createElement("video");
              videoElement.controls = true;
              getVideoUrl(result["video_name"], timestamp, videoElement);
              videoElement.style.width = "480px";
              videoElement.style.height = "270px";
              videoElement.style.borderRadius = "10px"; // Set the desired border radius

              videoContainer.appendChild(videoElement);

              const titleElement = document.createElement("p");
              titleElement.textContent = result["video_name"];
              titleElement.style.margin = "0px";
              titleElement.style.marginTop = "10px";
              titleElement.style.marginBottom = "5px";
              titleElement.style.color = "blue";
              videoContainer.appendChild(titleElement);

              const shot_startTime = millisecondsToTimeFormat(
                parseInt(result["shot_startTime"])
              );
              const shot_endTime = millisecondsToTimeFormat(
                parseInt(result["shot_endTime"])
              );
              const frameTimeElement = document.createElement("p");
              frameTimeElement.textContent = `Shot Time: ${shot_startTime} - ${shot_endTime}`;
              frameTimeElement.style.margin = "0px";
              frameTimeElement.style.marginBottom = "40px";
              videoContainer.appendChild(frameTimeElement);

              uniqueTimestamps.add(key);
              console.log("Score: " + result["score"]);
              console.log("Video name: " + result["video_name"]);
              console.log("Shot id: " + result["shot_id"]);
              console.log(
                "Shot public figures: " + result["shot_publicFigures"]
              );
              console.log(
                "Shot private figures: " + result["shot_privateFigures"]
              );
              console.log("Shot transcript: " + result["shot_transcript"]);
              console.log("Shot description: " + result["shot_description"]);
              console.log("========================");
            }
          });
        } else {
        }
      })
      .catch((error) => {
        console.error(error);
      });
  };
  fetchData();
}

function searchByImage(
  query: string,
  setSearching: {
    (value: React.SetStateAction<boolean>): void;
    (arg0: boolean): void;
  }
) {
  console.clear();
  const videoContainer = document.getElementById("search");
  if (!videoContainer) {
    console.error("Video container not found");
    return;
  }
  while (videoContainer.firstChild) {
    videoContainer.removeChild(videoContainer.firstChild);
  }
  setSearching(true);
  const uniqueTimestamps = new Set<string>();
  const fetchData = async () => {
    const response = await authenticatedAxios
      .post(AWS_API_URL + "/search", {
        type: "image",
        index: aoss_index,
        query: query,
      })
      .then((response) => {
        setSearching(false);
        if (response.status == 200) {
          const results = response.data;
          results.forEach((result: { [x: string]: string }) => {
            let timestamp = parseInt(result["shot_startTime"]);
            let key = result["video_name"] + timestamp;
            if (!uniqueTimestamps.has(key)) {
              const videoElement = document.createElement("video");
              videoElement.controls = true;
              getVideoUrl(result["video_name"], timestamp, videoElement);
              videoElement.style.width = "480px";
              videoElement.style.height = "270px";
              videoElement.style.borderRadius = "10px"; // Set the desired border radius

              videoContainer.appendChild(videoElement);

              const titleElement = document.createElement("p");
              titleElement.textContent = result["video_name"];
              titleElement.style.margin = "0px";
              titleElement.style.marginTop = "10px";
              titleElement.style.marginBottom = "5px";
              titleElement.style.color = "blue";
              videoContainer.appendChild(titleElement);

              const shot_startTime = millisecondsToTimeFormat(
                parseInt(result["shot_startTime"])
              );
              const shot_endTime = millisecondsToTimeFormat(
                parseInt(result["shot_endTime"])
              );
              const frameTimeElement = document.createElement("p");
              frameTimeElement.textContent = `Shot Time: ${shot_startTime} - ${shot_endTime}`;
              frameTimeElement.style.margin = "0px";
              frameTimeElement.style.marginBottom = "40px";
              videoContainer.appendChild(frameTimeElement);

              uniqueTimestamps.add(key);
              console.log("Score: " + result["score"]);
              console.log("Video name: " + result["video_name"]);
              console.log("Shot id: " + result["shot_id"]);
              console.log(
                "Shot public figures: " + result["shot_publicFigures"]
              );
              console.log(
                "Shot private figures: " + result["shot_privateFigures"]
              );
              console.log("Shot transcript: " + result["shot_transcript"]);
              console.log("Shot description: " + result["shot_description"]);
              console.log("========================");
            }
          });
        } else {
        }
      })
      .catch((error) => {
        console.error(error);
      });
  };
  fetchData();
}

function getVideoUrl(
  video_name: string,
  timestamp: number,
  videoElement: HTMLVideoElement
) {
  const fetchData = async () => {
    const response = await authenticatedAxios
      .get(
        AWS_API_URL + "/presignedurl_video?type=get&object_name=" + video_name
      )
      .then((response) => {
        if (response.status == 200) {
          var presignedUrl = response.data;
          videoElement.src = presignedUrl;
          videoElement.currentTime = (timestamp + 1) / 1000;
          videoElement.load();
        }
      })
      .catch((error) => {
        console.error(error);
      });
  };
  fetchData();
}

function getAllJobs(addItem: (item: TableData) => void) {
  const fetchData = async () => {
    const response = await authenticatedAxios
      .get(AWS_API_URL + "/get_all_jobs")
      .then((response) => {
        if (response.status == 200) {
          let jobs = response.data;
          jobs = jobs
            .slice()
            .sort((jobA: { Started: string }, jobB: { Started: string }) => {
              const startedA = jobA.Started as string;
              const startedB = jobB.Started as string;

              return (
                new Date(startedA).getTime() - new Date(startedB).getTime()
              );
            });
          for (let i = 0; i < jobs.length; i++) {
            jobIds.unshift(jobs[i]["JobId"]);
            jobStatuses.unshift(jobs[i]["Status"]);
            startTimes.unshift(jobs[i]["Started"]);
            endTimes.unshift(
              jobs[i]["EndTime"] === "-" ? "" : jobs[i]["EndTime"]
            );
            jobInputs.unshift(jobs[i]["Input"]);
            let item = {
              jobId: jobs[i]["JobId"],
              jobStatus: jobs[i]["Status"],
              startTime: jobs[i]["Started"],
              endTime: jobs[i]["EndTime"] === "-" ? "" : jobs[i]["EndTime"],
              jobInput: jobs[i]["Input"],
            };
            addItem(item);
          }
        }
      })
      .catch((error) => {
        console.error(error);
      });
  };
  fetchData();
}

function millisecondsToTimeFormat(ms: number): string {
  const hours = Math.floor((ms / 3600000) % 24);
  const minutes = Math.floor((ms / 60000) % 60);
  const seconds = Math.floor((ms / 1000) % 60);
  const milliseconds = ms % 1000;

  return `${hours.toString().padStart(2, "0")}:${minutes
    .toString()
    .padStart(2, "0")}:${seconds.toString().padStart(2, "0")}:${milliseconds
    .toString()
    .padStart(3, "0")}`;
}
