// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useRef, useState, useCallback, startTransition } from "react";
import Container from "@cloudscape-design/components/container";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Spinner from "@cloudscape-design/components/spinner";
import { useAuthenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import axios from "axios";
import { Auth } from "aws-amplify";
import { useParams, useNavigate } from "react-router-dom";
import { AWS_API_URL } from "../constants";
import ShotCard from "../components/shot-card";
import FaceGallery, { Face } from "../components/face-gallery";
import CostPanel, { CostEntry } from "../components/cost-panel";
import styled from "styled-components";

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

// Types for job detail data
interface JobDetail {
  jobId: string;
  status: string;
  input: string;
  started: string;
  endTime: string;
}

interface ShotFace {
  faceId: string;
  label: string;
  isCelebrity: boolean;
  faceImageUrl: string;
}

interface ShotData {
  shotId: string;
  startTime: number;
  endTime: number;
  frames: number[];
  frameUrls: string[];
  compositeUrl: string;
  description: string;
  faces: ShotFace[];
  transcript: string;
}

// Timeline virtualization constants
const CARD_WIDTH = 180;
const CARD_GAP = 12;
const CARD_STRIDE = CARD_WIDTH + CARD_GAP; // 192
const BUFFER = 3;

const ShotTimelineContainer = styled.div`
  overflow-x: auto;
  overflow-y: hidden;
  padding: 8px 0;
  height: 160px;
`;

const TimelineCard = styled.div<{ $isSelected: boolean }>`
  min-width: 180px;
  max-width: 180px;
  padding: 8px;
  border: 2px solid ${(props) => (props.$isSelected ? "#0073bb" : "#d5dbdb")};
  border-radius: 8px;
  background-color: ${(props) => (props.$isSelected ? "#f2f8fd" : "#fafafa")};
  cursor: pointer;
  flex-shrink: 0;
  text-align: center;
  transition:
    border-color 0.15s ease,
    background-color 0.15s ease;

  &:hover {
    border-color: #0073bb;
    background-color: #f2f8fd;
  }
`;

const TimelineThumbnail = styled.img`
  width: 100%;
  height: 80px;
  object-fit: cover;
  border-radius: 4px;
  background-color: #e9ebed;
  margin-bottom: 4px;
`;

const TimelineThumbnailPlaceholder = styled.div`
  width: 100%;
  height: 80px;
  background-color: #e9ebed;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #687078;
  font-size: 11px;
  margin-bottom: 4px;
`;

function millisecondsToTimeFormat(ms: number): string {
  const hours = Math.floor((ms / 3600000) % 24);
  const minutes = Math.floor((ms / 60000) % 60);
  const seconds = Math.floor((ms / 1000) % 60);

  return `${hours.toString().padStart(2, "0")}:${minutes
    .toString()
    .padStart(2, "0")}:${seconds.toString().padStart(2, "0")}`;
}

const TimelineItem = React.memo<{
  shot: ShotData;
  index: number;
  isSelected: boolean;
  onSelect: (index: number) => void;
}>(({ shot, index, isSelected, onSelect }) => {
  const thumbnailUrl = shot.frameUrls?.[0];
  return (
    <TimelineCard $isSelected={isSelected} onClick={() => onSelect(index)}>
      {thumbnailUrl ? (
        <TimelineThumbnail
          src={thumbnailUrl}
          alt={`Shot ${shot.shotId}`}
          loading="lazy"
        />
      ) : (
        <TimelineThumbnailPlaceholder>
          No thumbnail
        </TimelineThumbnailPlaceholder>
      )}
      <Box fontSize="body-s" fontWeight="bold">
        {shot.shotId}
      </Box>
      <Box fontSize="body-s" color="text-body-secondary">
        {millisecondsToTimeFormat(shot.startTime)} -{" "}
        {millisecondsToTimeFormat(shot.endTime)}
      </Box>
    </TimelineCard>
  );
});

const AdminDetail: React.FC = () => {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const { user } = useAuthenticator((context) => [context.user]);
  const videoRef = useRef<HTMLVideoElement>(null);
  const shotsRef = useRef<ShotData[]>([]);
  const timelineRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef(0);

  // State
  const [isLoading, setIsLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<JobDetail | null>(null);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [shots, setShots] = useState<ShotData[]>([]);
  const [selectedShotIndex, setSelectedShotIndex] = useState<number>(0);
  const [faces, setFaces] = useState<Face[]>([]);
  const [costEntries, setCostEntries] = useState<CostEntry[]>([]);
  const [totalCost, setTotalCost] = useState(0);
  const [visibleRange, setVisibleRange] = useState({ start: 0, end: 20 });

  // Load job detail on mount
  useEffect(() => {
    if (jobId) {
      loadJobDetail(jobId);
    }
  }, [jobId]);

  const loadJobDetail = async (id: string) => {
    setIsLoading(true);
    setErrorMessage(null);

    try {
      // Fetch job info
      const jobResponse = await authenticatedAxios.get(
        AWS_API_URL + "/get_all_jobs",
      );

      if (jobResponse.status === 200) {
        const jobs = jobResponse.data;
        const job = jobs.find((j: { JobId: string }) => j.JobId === id);

        if (job) {
          setJobDetail({
            jobId: job.JobId,
            status: job.Status,
            input: job.Input,
            started: job.Started,
            endTime: job.EndTime === "-" ? "" : job.EndTime,
          });

          // Load video presigned URL
          await loadVideoUrl(job.Input);

          // Load shot data from the job's collection files
          await loadShotData(id);
        } else {
          setErrorMessage("Job not found.");
        }
      }
    } catch (error) {
      console.error("Error loading job detail:", error);
      setErrorMessage("Failed to load job details. Please try again.");
    } finally {
      setIsLoading(false);
    }
  };

  const loadVideoUrl = async (videoName: string) => {
    try {
      const response = await authenticatedAxios.get(
        AWS_API_URL + "/presignedurl_video?type=get&object_name=" + videoName,
      );
      if (response.status === 200) {
        setVideoUrl(response.data);
      }
    } catch (error) {
      console.error("Error loading video URL:", error);
    }
  };

  const loadShotData = async (id: string) => {
    try {
      const response = await authenticatedAxios.get(
        AWS_API_URL + "/admin/jobs/" + id + "/shots",
      );

      if (response.status === 200) {
        const apiShots = response.data as Array<Record<string, unknown>>;

        const mappedShots: ShotData[] = apiShots.map((s) => ({
          shotId: (s.shot_id as string) || "",
          startTime: Number(s.shot_startTime) || 0,
          endTime: Number(s.shot_endTime) || 0,
          frames: (s.frames as number[]) || [],
          frameUrls: (s.frameUrls as string[]) || [],
          compositeUrl: (s.compositeUrl as string) || "",
          description: (s.shot_description as string) || "",
          faces: ((s.faces as ShotFace[]) || []).map((f) => ({
            faceId: f.faceId || "",
            label: f.label || "",
            isCelebrity: Boolean(f.isCelebrity),
            faceImageUrl: f.faceImageUrl || "",
          })),
          transcript: (s.shot_transcript as string) || "",
        }));

        setShots(mappedShots);

        // Extract unique faces from all shots by faceId or label (for celebrities)
        const allFaces: Face[] = [];
        const seenKeys = new Set<string>();
        mappedShots.forEach((shot) => {
          shot.faces.forEach((face) => {
            const key = face.faceId || face.label;
            if (key && !seenKeys.has(key)) {
              seenKeys.add(key);
              allFaces.push({
                faceId: face.faceId,
                label: face.label,
                isCelebrity: face.isCelebrity,
                imageUrl: face.faceImageUrl || "",
              });
            }
          });
        });
        setFaces(allFaces);
      }
    } catch (error) {
      console.error("Error loading shot data:", error);
      setErrorMessage("Failed to load shot data.");
    }

    // Cost data loading placeholder
    setCostEntries([]);
    setTotalCost(0);
  };

  // Keep ref in sync so the stable callback reads fresh data
  shotsRef.current = shots;

  const handleShotSelect = useCallback((index: number) => {
    const shot = shotsRef.current[index];
    if (shot && videoRef.current) {
      videoRef.current.currentTime = shot.startTime / 1000;
    }
    startTransition(() => {
      setSelectedShotIndex(index);
    });
  }, []);

  // Pause the video when it reaches the selected shot's endTime
  const selectedEndTime = shots[selectedShotIndex]?.endTime ?? 0;

  useEffect(() => {
    const videoEl = videoRef.current;
    if (!videoEl || selectedEndTime <= 0) return;

    const checkTime = () => {
      if (videoEl.currentTime >= selectedEndTime / 1000) {
        videoEl.pause();
      }
    };
    videoEl.addEventListener("timeupdate", checkTime);

    return () => {
      videoEl.removeEventListener("timeupdate", checkTime);
    };
  }, [selectedShotIndex, selectedEndTime]);

  // Compute visible range from scroll position
  const computeVisibleRange = useCallback((container: HTMLDivElement, total: number) => {
    const scrollLeft = container.scrollLeft;
    const clientWidth = container.clientWidth;
    const start = Math.max(0, Math.floor(scrollLeft / CARD_STRIDE) - BUFFER);
    const end = Math.min(total, Math.ceil((scrollLeft + clientWidth) / CARD_STRIDE) + BUFFER);
    return { start, end };
  }, []);

  const handleTimelineScroll = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      const container = timelineRef.current;
      if (!container) return;
      const total = shotsRef.current.length;
      setVisibleRange((prev) => {
        const next = computeVisibleRange(container, total);
        if (prev.start === next.start && prev.end === next.end) return prev;
        return next;
      });
    });
  }, [computeVisibleRange]);

  // Initialize visible range on mount / when shots load
  useEffect(() => {
    const container = timelineRef.current;
    if (!container || shots.length === 0) return;
    setVisibleRange(computeVisibleRange(container, shots.length));
  }, [shots.length, computeVisibleRange]);

  // Auto-scroll to selected shot
  useEffect(() => {
    const container = timelineRef.current;
    if (!container || shots.length === 0) return;
    const targetLeft = selectedShotIndex * CARD_STRIDE;
    const targetRight = targetLeft + CARD_WIDTH;
    if (targetLeft < container.scrollLeft) {
      container.scrollTo({ left: targetLeft - CARD_GAP, behavior: "smooth" });
    } else if (targetRight > container.scrollLeft + container.clientWidth) {
      container.scrollTo({ left: targetRight - container.clientWidth + CARD_GAP, behavior: "smooth" });
    }
  }, [selectedShotIndex, shots.length]);

  const handleSaveShotDescription = async (
    shotId: string,
    newDescription: string,
  ) => {
    try {
      await authenticatedAxios.put(
        AWS_API_URL +
          "/admin/shots/" +
          encodeURIComponent(jobId + "_" + shotId) +
          "/metadata",
        { shot_description: newDescription },
      );
      setShots((prev) =>
        prev.map((shot) =>
          shot.shotId === shotId
            ? { ...shot, description: newDescription }
            : shot,
        ),
      );
    } catch (error) {
      console.error("Error saving shot description:", error);
      setErrorMessage("Failed to save description. Please try again.");
    }
  };

  const handleSaveShotTranscript = async (
    shotId: string,
    newTranscript: string,
  ) => {
    try {
      await authenticatedAxios.put(
        AWS_API_URL +
          "/admin/shots/" +
          encodeURIComponent(jobId + "_" + shotId) +
          "/metadata",
        { shot_transcript: newTranscript },
      );
      setShots((prev) =>
        prev.map((shot) =>
          shot.shotId === shotId
            ? { ...shot, transcript: newTranscript }
            : shot,
        ),
      );
    } catch (error) {
      console.error("Error saving shot transcript:", error);
      setErrorMessage("Failed to save transcript. Please try again.");
    }
  };

  const handleLabelFace = async (faceId: string, label: string) => {
    try {
      await authenticatedAxios.put(
        AWS_API_URL + "/admin/faces/" + encodeURIComponent(faceId) + "/label",
        { label },
      );
      // Update local state on success
      setFaces((prev) =>
        prev.map((face) =>
          face.faceId === faceId ? { ...face, label } : face,
        ),
      );
      // Also update faces within shots — only spread shots that contain the face
      setShots((prev) =>
        prev.map((shot) => {
          const hasFace = shot.faces.some((f) => f.faceId === faceId);
          if (!hasFace) return shot;
          return {
            ...shot,
            faces: shot.faces.map((f) =>
              f.faceId === faceId ? { ...f, label } : f,
            ),
          };
        }),
      );
    } catch (error) {
      console.error("Error labeling face:", error);
      setErrorMessage("Failed to update face label. Please try again.");
    }
  };

  const handleDeleteFace = async (faceId: string) => {
    await authenticatedAxios.delete(
      AWS_API_URL + "/admin/faces/" + encodeURIComponent(faceId),
    );
    setFaces((prev) => prev.filter((f) => f.faceId !== faceId));
    setShots((prev) =>
      prev.map((shot) => {
        const hasFace = shot.faces.some((f) => f.faceId === faceId);
        if (!hasFace) return shot;
        return { ...shot, faces: shot.faces.filter((f) => f.faceId !== faceId) };
      }),
    );
  };

  if (isLoading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box variant="p" padding={{ top: "s" }}>
            Loading job details...
          </Box>
        </Box>
      </Container>
    );
  }

  if (errorMessage && !jobDetail) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{errorMessage}</Alert>
        <Button onClick={() => navigate("/admin")}>Back to Admin</Button>
      </SpaceBetween>
    );
  }

  const selectedShot = shots[selectedShotIndex] || null;

  return (
    <SpaceBetween size="l">
      {errorMessage && (
        <Alert type="error" dismissible onDismiss={() => setErrorMessage(null)}>
          {errorMessage}
        </Alert>
      )}

      {/* Header with back button */}
      <Header
        variant="h1"
        actions={
          <Button variant="link" onClick={() => navigate("/admin")}>
            Back to Jobs
          </Button>
        }
        description={
          jobDetail
            ? `Status: ${jobDetail.status} | Started: ${jobDetail.started}`
            : ""
        }
      >
        {jobDetail?.input || "Job Detail"}
      </Header>

      {/* Job info summary */}
      {jobDetail && (
        <Container header={<Header variant="h2">Job Information</Header>}>
          <ColumnLayout columns={4} variant="text-grid">
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Job ID</Box>
              <Box>{jobDetail.jobId}</Box>
            </SpaceBetween>
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Status</Box>
              <StatusIndicator
                type={
                  jobDetail.status.toLowerCase() === "completed" ||
                  jobDetail.status.toLowerCase() === "succeeded"
                    ? "success"
                    : jobDetail.status.toLowerCase() === "failed"
                      ? "error"
                      : "loading"
                }
              >
                {jobDetail.status}
              </StatusIndicator>
            </SpaceBetween>
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Started</Box>
              <Box>{jobDetail.started}</Box>
            </SpaceBetween>
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">End Time</Box>
              <Box>{jobDetail.endTime || "-"}</Box>
            </SpaceBetween>
          </ColumnLayout>
        </Container>
      )}

      {/* Video player */}
      <Container header={<Header variant="h2">Video</Header>}>
        {videoUrl ? (
          <Box textAlign="center">
            <video
              ref={videoRef}
              src={videoUrl}
              controls
              preload="metadata"
              style={{
                width: "100%",
                maxWidth: "960px",
                borderRadius: "8px",
                backgroundColor: "#000",
              }}
            />
          </Box>
        ) : (
          <Box textAlign="center" padding="l" color="text-status-inactive">
            Video preview is not available.
          </Box>
        )}
      </Container>

      {/* Shot timeline */}
      {jobDetail && (
        <Container
          header={
            <Header
              variant="h2"
              counter={shots.length > 0 ? `(${shots.length} shots)` : undefined}
              description={
                shots.length > 0
                  ? "Click a shot to seek the video and view details below."
                  : undefined
              }
            >
              Shot Timeline
            </Header>
          }
        >
          {shots.length === 0 ? (
            <Box textAlign="center" padding="l" color="text-status-inactive">
              No shot data available. The indexing pipeline may still be
              running, or shot data has not been generated yet.
            </Box>
          ) : (
            <SpaceBetween size="m">
              {/* Horizontal scrollable virtualized timeline */}
              <ShotTimelineContainer ref={timelineRef} onScroll={handleTimelineScroll}>
                <div style={{
                  width: shots.length * CARD_STRIDE - CARD_GAP,
                  position: "relative",
                  height: "100%",
                }}>
                  {shots.slice(visibleRange.start, visibleRange.end).map((shot, i) => {
                    const index = visibleRange.start + i;
                    return (
                      <div
                        key={shot.shotId}
                        style={{
                          position: "absolute",
                          left: index * CARD_STRIDE,
                          width: CARD_WIDTH,
                          top: 0,
                        }}
                      >
                        <TimelineItem
                          shot={shot}
                          index={index}
                          isSelected={selectedShotIndex === index}
                          onSelect={handleShotSelect}
                        />
                      </div>
                    );
                  })}
                </div>
              </ShotTimelineContainer>

              {/* Only show the selected shot's detail card */}
              {selectedShot && (
                <ShotCard
                  key={selectedShot.shotId}
                  shotId={selectedShot.shotId}
                  startTime={selectedShot.startTime}
                  endTime={selectedShot.endTime}
                  compositeUrl={selectedShot.compositeUrl}
                  description={selectedShot.description}
                  faces={selectedShot.faces}
                  transcript={selectedShot.transcript}
                  onSave={(newDescription) =>
                    handleSaveShotDescription(
                      selectedShot.shotId,
                      newDescription,
                    )
                  }
                  onSaveTranscript={(newTranscript) =>
                    handleSaveShotTranscript(
                      selectedShot.shotId,
                      newTranscript,
                    )
                  }
                  onLabelFace={handleLabelFace}
                  onDeleteFace={handleDeleteFace}
                />
              )}
            </SpaceBetween>
          )}
        </Container>
      )}

      {/* Face gallery */}
      <FaceGallery faces={faces} onLabelFace={handleLabelFace} onDeleteFace={handleDeleteFace} />

      {/* Cost panel */}
      <CostPanel entries={costEntries} totalCostUsd={totalCost} />
    </SpaceBetween>
  );
};

export default AdminDetail;
