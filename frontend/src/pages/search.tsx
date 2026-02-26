// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState, useCallback, useRef, useEffect } from "react";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Input from "@cloudscape-design/components/input";
import FormField from "@cloudscape-design/components/form-field";
import FileUpload from "@cloudscape-design/components/file-upload";
import Alert from "@cloudscape-design/components/alert";
import Tabs from "@cloudscape-design/components/tabs";
import Button from "@cloudscape-design/components/button";
import Spinner from "@cloudscape-design/components/spinner";
import { useAuthenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import axios from "axios";
import { Auth } from "aws-amplify";
import { AWS_API_URL } from "../constants";
import SearchResults, { SearchResult } from "../components/search-results";

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

const Search: React.FC = () => {
  const { user } = useAuthenticator((context) => [context.user]);
  const userId = user?.username;

  // Search state
  const [textQuery, setTextQuery] = useState("");
  const [imageFiles, setImageFiles] = useState<File[]>([]);
  const [clipFiles, setClipFiles] = useState<File[]>([]);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [activeTabId, setActiveTabId] = useState("text");

  // Shared player state
  const [selectedShotIndex, setSelectedShotIndex] = useState<number | null>(
    null,
  );
  const videoRef = useRef<HTMLVideoElement>(null);
  const endTimeRef = useRef<number>(0);
  const currentVideoNameRef = useRef<string>("");
  const videoUrlsRef = useRef<Record<string, string>>({});
  const pendingVideoRef = useRef<{
    startTime: number;
    metadataLoaded: boolean;
    loadId: number;
  } | null>(null);
  const loadIdRef = useRef<number>(0);

  // Floating video position state
  const [videoPos, setVideoPos] = useState<{
    top: number;
    left: number;
    width: number;
    height: number;
  } | null>(null);
  const [isVideoLoading, setIsVideoLoading] = useState(false);
  const gridWrapperRef = useRef<HTMLDivElement>(null);
  const thumbnailRefsRef = useRef<Map<number, HTMLDivElement>>(new Map());

  // Card ref callback for tracking thumbnail elements
  const cardRefCallback = useCallback(
    (index: number, el: HTMLDivElement | null) => {
      if (el) {
        thumbnailRefsRef.current.set(index, el);
      } else {
        thumbnailRefsRef.current.delete(index);
      }
    },
    [],
  );

  // Pre-fetch presigned URLs for all unique videos in results
  useEffect(() => {
    const uniqueNames = [...new Set(results.map((r) => r.video_name))];
    const toFetch = uniqueNames.filter(
      (name) => !videoUrlsRef.current[name],
    );
    if (toFetch.length === 0) return;

    let cancelled = false;

    const fetchUrls = async () => {
      await Promise.all(
        toFetch.map(async (videoName) => {
          try {
            const response = await authenticatedAxios.get(
              AWS_API_URL +
                "/presignedurl_video?type=get&object_name=" +
                videoName,
            );
            if (!cancelled && response.status === 200) {
              videoUrlsRef.current[videoName] = response.data;
            }
          } catch (error) {
            console.error("Error loading video URL for", videoName, error);
          }
        }),
      );
    };

    fetchUrls();
    return () => {
      cancelled = true;
    };
  }, [results]);

  // Pause video at shot end boundary
  useEffect(() => {
    const videoEl = videoRef.current;
    if (!videoEl) return;

    const checkTime = () => {
      if (
        endTimeRef.current > 0 &&
        videoEl.currentTime >= endTimeRef.current / 1000
      ) {
        videoEl.pause();
      }
    };
    videoEl.addEventListener("timeupdate", checkTime);
    return () => {
      videoEl.removeEventListener("timeupdate", checkTime);
    };
  }, []);

  // Reset player when results change (new search)
  useEffect(() => {
    setSelectedShotIndex(null);
    setVideoPos(null);
    setIsVideoLoading(false);
    currentVideoNameRef.current = "";
    pendingVideoRef.current = null;
    loadIdRef.current = 0;
  }, [results]);

  // Recalculate video position on window resize
  useEffect(() => {
    if (selectedShotIndex === null) return;

    const handleResize = () => {
      const thumbEl = thumbnailRefsRef.current.get(selectedShotIndex);
      const wrapperEl = gridWrapperRef.current;
      if (thumbEl && wrapperEl) {
        const thumbRect = thumbEl.getBoundingClientRect();
        const wrapperRect = wrapperEl.getBoundingClientRect();
        setVideoPos({
          top: thumbRect.top - wrapperRect.top,
          left: thumbRect.left - wrapperRect.left,
          width: thumbRect.width,
          height: thumbRect.height,
        });
      }
    };

    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [selectedShotIndex]);

  // Handle shot selection — load or seek the shared player
  const handleSelectShot = useCallback(
    async (result: SearchResult, index: number, thumbnailRect: DOMRect) => {
      setSelectedShotIndex(index);

      // Position video over the thumbnail
      const wrapperRect = gridWrapperRef.current?.getBoundingClientRect();
      if (wrapperRect) {
        setVideoPos({
          top: thumbnailRect.top - wrapperRect.top,
          left: thumbnailRect.left - wrapperRect.left,
          width: thumbnailRect.width,
          height: thumbnailRect.height,
        });
      }

      const videoEl = videoRef.current;
      if (!videoEl) return;

      // Get presigned URL (should be pre-fetched, fetch on demand if not)
      let url = videoUrlsRef.current[result.video_name];
      if (!url) {
        try {
          const response = await authenticatedAxios.get(
            AWS_API_URL +
              "/presignedurl_video?type=get&object_name=" +
              result.video_name,
          );
          if (response.status === 200) {
            url = response.data;
            videoUrlsRef.current[result.video_name] = url;
          }
        } catch (error) {
          console.error("Error loading video URL:", error);
          return;
        }
      }
      if (!url) return;

      endTimeRef.current = result.shot_endTime;

      const isSameVideo = currentVideoNameRef.current === result.video_name;
      const thisLoadId = ++loadIdRef.current;

      // Hide the video and show spinner while seeking/loading
      setIsVideoLoading(true);

      if (isSameVideo) {
        // Same video — just seek to the new shot
        pendingVideoRef.current = {
          startTime: result.shot_startTime,
          metadataLoaded: true,
          loadId: thisLoadId,
        };
        videoEl.currentTime = (result.shot_startTime + 1) / 1000;
      } else {
        // Different video — load new source
        currentVideoNameRef.current = result.video_name;
        videoEl.pause();
        pendingVideoRef.current = {
          startTime: result.shot_startTime,
          metadataLoaded: false,
          loadId: thisLoadId,
        };
        videoEl.src = url;
      }
    },
    [],
  );

  // Deduplicate results by video_name + shot_startTime
  const deduplicateResults = (rawResults: any[]): SearchResult[] => {
    const seen = new Set<string>();
    const deduplicated: SearchResult[] = [];

    for (const result of rawResults) {
      const startTime = parseInt(result["shot_startTime"]);
      const key = result["video_name"] + startTime;
      if (!seen.has(key)) {
        seen.add(key);
        deduplicated.push({
          jobId: result["jobId"] || "",
          video_name: result["video_name"],
          shot_id: result["shot_id"] || "",
          shot_startTime: startTime,
          shot_endTime: parseInt(result["shot_endTime"]) || 0,
          shot_description: result["shot_description"] || "",
          shot_publicFigures: result["shot_publicFigures"] || "",
          shot_faces:
            result["shot_privateFigures"] || result["shot_faces"] || "",
          shot_transcript: result["shot_transcript"] || "",
          score: parseFloat(result["score"]) || 0,
          composite_url: result["composite_url"] || "",
        });
      }
    }

    return deduplicated;
  };

  // Text search
  const handleTextSearch = useCallback(async () => {
    if (!textQuery.trim()) return;

    setIsLoading(true);
    setErrorMessage(null);
    setResults([]);

    try {
      const response = await authenticatedAxios.get(
        AWS_API_URL +
          "/search?type=text&query=" +
          encodeURIComponent(textQuery),
      );
      if (response.status === 200) {
        setResults(deduplicateResults(response.data));
      }
    } catch (error) {
      console.error("Text search error:", error);
      setErrorMessage("An error occurred while searching. Please try again.");
    } finally {
      setIsLoading(false);
      setHasSearched(true);
    }
  }, [textQuery]);

  // Image search
  const handleImageSearch = useCallback(async (files: File[]) => {
    if (files.length === 0) return;

    const file = files[0];
    setIsLoading(true);
    setErrorMessage(null);
    setResults([]);

    try {
      const base64String = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result as string);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });

      const response = await authenticatedAxios.post(AWS_API_URL + "/search", {
        type: "image",
        query: base64String,
      });

      if (response.status === 200) {
        setResults(deduplicateResults(response.data));
      }
    } catch (error) {
      console.error("Image search error:", error);
      setErrorMessage(
        "An error occurred while searching by image. Please try again.",
      );
    } finally {
      setIsLoading(false);
      setHasSearched(true);
      setImageFiles([]);
    }
  }, []);

  // Clip search - upload then search
  const handleClipSearch = useCallback(
    async (files: File[]) => {
      if (files.length === 0 || !userId) return;

      const clipFile = files[0];
      const clipFileName = userId + clipFile.name;
      const allowedFilenameRegex = /^[a-zA-Z0-9._ -]+\.(mp4)$/;

      if (!allowedFilenameRegex.test(clipFileName)) {
        setErrorMessage(
          "Invalid filename. Please use alphanumeric characters.",
        );
        return;
      }

      setIsLoading(true);
      setErrorMessage(null);
      setResults([]);

      try {
        // Step 1: Get presigned URL for clip upload
        const presignedResponse = await authenticatedAxios.get(
          AWS_API_URL +
            "/presignedurl_video?type=clipsearch&object_name=" +
            clipFileName,
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
          formData.append("file", clipFile);

          // Step 2: Upload clip to S3
          const uploadResponse = await axios.post(presignedUrl, formData);

          if (uploadResponse.status === 204) {
            // Step 3: Search by clip
            const searchResponse = await authenticatedAxios.get(
              AWS_API_URL + "/search?type=clip&query=" + clipFileName,
            );

            if (searchResponse.status === 200) {
              setResults(deduplicateResults(searchResponse.data));
            }
          }
        }
      } catch (error) {
        console.error("Clip search error:", error);
        setErrorMessage(
          "An error occurred while searching by clip. Please try again.",
        );
      } finally {
        setIsLoading(false);
        setHasSearched(true);
        setClipFiles([]);
      }
    },
    [userId],
  );

  return (
    <SpaceBetween size="l">
      <div>
        <Box variant="h1" padding={{ bottom: "xs" }}>
          Video Search
        </Box>
        <Box variant="p" color="text-body-secondary" padding={{ bottom: "m" }}>
          Search your video library using natural language, images, or video
          clips
        </Box>
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: "text",
              label: "Text Search",
              content: (
                <SpaceBetween size="m">
                  <FormField description="Enter a description of what you are looking for">
                    <div
                      style={{
                        display: "flex",
                        gap: "8px",
                        alignItems: "center",
                      }}
                    >
                      <div style={{ flex: 1 }}>
                        <Input
                          value={textQuery}
                          onChange={({ detail }) => setTextQuery(detail.value)}
                          onKeyDown={(event) => {
                            if (event.detail.key === "Enter") {
                              handleTextSearch();
                            }
                          }}
                          placeholder="Search"
                          type="search"
                        />
                      </div>
                      <Button
                        variant="primary"
                        onClick={handleTextSearch}
                        loading={isLoading && activeTabId === "text"}
                        iconName="search"
                      >
                        Search
                      </Button>
                    </div>
                  </FormField>
                </SpaceBetween>
              ),
            },
            {
              id: "image",
              label: "Image Search",
              content: (
                <div style={{ paddingTop: "8px", maxWidth: "480px" }}>
                  <FormField description="Upload an image to find similar scenes in your video library">
                    <FileUpload
                      onChange={({ detail }) => {
                        const files = detail.value as unknown as File[];
                        setImageFiles(files);
                        if (files.length > 0) {
                          handleImageSearch(files);
                        }
                      }}
                      value={imageFiles as any}
                      accept=".jpeg, .jpg, .png"
                      i18nStrings={{
                        uploadButtonText: (e) =>
                          e ? "Choose images" : "Choose image",
                        dropzoneText: (e) =>
                          e
                            ? "Drop images to search"
                            : "Drop an image to search",
                        removeFileAriaLabel: (e) => `Remove file ${e + 1}`,
                        limitShowFewer: "Show fewer files",
                        limitShowMore: "Show more files",
                        errorIconAriaLabel: "Error",
                      }}
                      constraintText="Supported formats: JPEG, JPG, PNG"
                    />
                  </FormField>
                </div>
              ),
            },
            {
              id: "clip",
              label: "Clip Search",
              content: (
                <div style={{ paddingTop: "8px", maxWidth: "480px" }}>
                  <FormField description="Upload an MP4 clip to find the source video in your library">
                    <FileUpload
                      onChange={({ detail }) => {
                        const files = detail.value as unknown as File[];
                        setClipFiles(files);
                        if (files.length > 0) {
                          handleClipSearch(files);
                        }
                      }}
                      value={clipFiles as any}
                      accept=".mp4"
                      i18nStrings={{
                        uploadButtonText: (e) =>
                          e ? "Choose clips" : "Choose clip",
                        dropzoneText: (e) =>
                          e ? "Drop clips to search" : "Drop a clip to search",
                        removeFileAriaLabel: (e) => `Remove file ${e + 1}`,
                        limitShowFewer: "Show fewer files",
                        limitShowMore: "Show more files",
                        errorIconAriaLabel: "Error",
                      }}
                      constraintText="Supported format: MP4"
                    />
                  </FormField>
                </div>
              ),
            },
          ]}
        />
      </div>

      {errorMessage && (
        <Alert type="error" dismissible onDismiss={() => setErrorMessage(null)}>
          {errorMessage}
        </Alert>
      )}

      {/* Floating video player positioned over active card's thumbnail */}
      <div ref={gridWrapperRef} style={{ position: "relative" }}>
        <video
          ref={videoRef}
          controls
          preload="metadata"
          onLoadedMetadata={() => {
            const pending = pendingVideoRef.current;
            const videoEl = videoRef.current;
            if (pending && !pending.metadataLoaded && videoEl
                && pending.loadId === loadIdRef.current) {
              pending.metadataLoaded = true;
              videoEl.currentTime = (pending.startTime + 1) / 1000;
            }
          }}
          onSeeked={() => {
            const pending = pendingVideoRef.current;
            const videoEl = videoRef.current;
            if (pending && pending.metadataLoaded && videoEl
                && pending.loadId === loadIdRef.current) {
              pendingVideoRef.current = null;
              setIsVideoLoading(false);
              videoEl.play().catch(() => {});
            }
          }}
          style={{
            position: "absolute",
            top: videoPos?.top ?? 0,
            left: videoPos?.left ?? 0,
            width: videoPos?.width ?? 0,
            height: videoPos?.height ?? 0,
            zIndex: 10,
            borderRadius: "6px 6px 0 0",
            backgroundColor: "#000",
            visibility: videoPos && selectedShotIndex !== null && !isVideoLoading ? "visible" : "hidden",
            pointerEvents: videoPos && selectedShotIndex !== null && !isVideoLoading ? "auto" : "none",
          }}
        />
        {isVideoLoading && videoPos && selectedShotIndex !== null && (
          <div
            style={{
              position: "absolute",
              top: videoPos.top,
              left: videoPos.left,
              width: videoPos.width,
              height: videoPos.height,
              zIndex: 11,
              borderRadius: "6px 6px 0 0",
              backgroundColor: "rgba(0, 0, 0, 0.5)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              pointerEvents: "none",
            }}
          >
            <span style={{ color: "#ffffff" }}>
              <Spinner size="large" />
            </span>
          </div>
        )}
        <SearchResults
          results={results}
          isLoading={isLoading}
          hasSearched={hasSearched}
          activeIndex={selectedShotIndex}
          onSelectShot={handleSelectShot}
          cardRefCallback={cardRefCallback}
          isVideoLoading={isVideoLoading}
        />
      </div>
    </SpaceBetween>
  );
};

export default Search;
