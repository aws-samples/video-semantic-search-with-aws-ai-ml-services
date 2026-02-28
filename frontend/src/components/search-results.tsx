// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useCallback } from "react";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Spinner from "@cloudscape-design/components/spinner";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import styled from "styled-components";

export interface SearchResult {
  jobId: string;
  video_name: string;
  shot_id: string;
  shot_startTime: number;
  shot_endTime: number;
  shot_description: string;
  shot_publicFigures: string;
  shot_faces: string;
  shot_transcript: string;
  score: number;
  composite_url: string;
}

export interface SearchResultsProps {
  results: SearchResult[];
  isLoading: boolean;
  hasSearched: boolean;
  activeIndex: number | null;
  onSelectShot: (result: SearchResult, index: number, thumbnailRect: DOMRect) => void;
  cardRefCallback: (index: number, el: HTMLDivElement | null) => void;
  isVideoLoading: boolean;
}

const ResultsGrid = styled.div`
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;

  @media (max-width: 1200px) {
    grid-template-columns: repeat(2, 1fr);
  }

  @media (max-width: 700px) {
    grid-template-columns: 1fr;
  }
`;

const ResultCard = styled.div<{ $isActive: boolean }>`
  border: 2px solid ${(props) => (props.$isActive ? "#0073bb" : "#d5dbdb")};
  border-radius: 8px;
  background-color: ${(props) => (props.$isActive ? "#f2f8fd" : "#ffffff")};
  cursor: pointer;
  overflow: hidden;
  transition:
    border-color 0.15s ease,
    background-color 0.15s ease;

  &:hover {
    border-color: #0073bb;
    background-color: #f2f8fd;
  }
`;

const ThumbnailContainer = styled.div`
  position: relative;
  aspect-ratio: 16 / 9;
  background-color: #1a1a2e;
  overflow: hidden;
`;

const ThumbnailImage = styled.img`
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
`;

const ThumbnailPlaceholder = styled.div`
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #687078;
  font-size: 14px;
`;

const PlayOverlay = styled.div<{ $isActive: boolean }>`
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: rgba(0, 0, 0, 0.35);
  opacity: ${(props) => (props.$isActive ? 0 : 0)};
  transition: opacity 0.15s ease;
  pointer-events: none;

  ${ResultCard}:hover & {
    opacity: ${(props) => (props.$isActive ? 0 : 1)};
  }
`;

const PlayButton = styled.div`
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background-color: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;

  &::after {
    content: "";
    display: block;
    width: 0;
    height: 0;
    border-style: solid;
    border-width: 10px 0 10px 18px;
    border-color: transparent transparent transparent #ffffff;
    margin-left: 3px;
  }
`;

const LoadingOverlay = styled.div`
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: rgba(0, 0, 0, 0.5);
  z-index: 1;
`;

const ScoreBadgeOverlay = styled.div`
  position: absolute;
  top: 6px;
  right: 6px;
  background-color: rgba(0, 0, 0, 0.7);
  color: #4ade80;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
`;

const CardBody = styled.div`
  padding: 10px 12px 12px;
`;

function millisecondsToTimeFormat(ms: number): string {
  const hours = Math.floor((ms / 3600000) % 24);
  const minutes = Math.floor((ms / 60000) % 60);
  const seconds = Math.floor((ms / 1000) % 60);
  const milliseconds = ms % 1000;

  return `${hours.toString().padStart(2, "0")}:${minutes
    .toString()
    .padStart(2, "0")}:${seconds.toString().padStart(2, "0")}.${milliseconds
    .toString()
    .padStart(3, "0")}`;
}

const SearchResults: React.FC<SearchResultsProps> = ({
  results,
  isLoading,
  hasSearched,
  activeIndex,
  onSelectShot,
  cardRefCallback,
  isVideoLoading,
}) => {
  const [failedImages, setFailedImages] = React.useState<Set<number>>(new Set());

  // Reset failed images when results change
  React.useEffect(() => {
    setFailedImages(new Set());
  }, [results]);

  const handleImageError = useCallback((index: number) => {
    setFailedImages((prev) => new Set(prev).add(index));
  }, []);

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" padding={{ top: "s" }}>
          Searching...
        </Box>
      </Box>
    );
  }

  if (results.length === 0) {
    if (!hasSearched) return null;
    return (
      <Box textAlign="center" padding="xxl" color="text-body-secondary">
        <Box variant="h3">No results found</Box>
        <Box variant="p" padding={{ top: "xxs" }}>
          Try a different search query or upload a different image/clip.
        </Box>
      </Box>
    );
  }

  return (
    <div>
      <Box variant="h2" padding={{ bottom: "s" }}>
        Search Results{" "}
        <Box variant="span" color="text-body-secondary" fontSize="heading-s">
          ({results.length})
        </Box>
      </Box>
      <ResultsGrid>
        {results.map((result, index) => {
          const hasThumb = !!result.composite_url && !failedImages.has(index);
          const isActive = activeIndex === index;

          return (
            <ResultCard
              key={`${result.jobId}-${result.shot_id}-${index}`}
              $isActive={isActive}
              onClick={() => {
                const thumbEl = document.querySelector(
                  `[data-card-index="${index}"]`,
                );
                onSelectShot(
                  result,
                  index,
                  thumbEl?.getBoundingClientRect() ?? new DOMRect(),
                );
              }}
            >
              <ThumbnailContainer
                data-thumbnail
                data-card-index={index}
                ref={(el) => cardRefCallback(index, el)}
              >
                {hasThumb ? (
                  <ThumbnailImage
                    src={result.composite_url}
                    alt={result.video_name}
                    loading="lazy"
                    onError={() => handleImageError(index)}
                  />
                ) : (
                  <ThumbnailPlaceholder>No thumbnail</ThumbnailPlaceholder>
                )}
                {isActive && isVideoLoading ? (
                  <LoadingOverlay>
                    <Spinner size="large" />
                  </LoadingOverlay>
                ) : (
                  <PlayOverlay $isActive={isActive}>
                    <PlayButton />
                  </PlayOverlay>
                )}
                <ScoreBadgeOverlay>{result.score.toFixed(2)}</ScoreBadgeOverlay>
              </ThumbnailContainer>
              <CardBody>
                <SpaceBetween size="xxs">
                  <Box variant="h4">{result.video_name}</Box>
                  <Box fontSize="body-s" color="text-body-secondary">
                    {millisecondsToTimeFormat(result.shot_startTime)} —{" "}
                    {millisecondsToTimeFormat(result.shot_endTime)}
                  </Box>
                  {result.shot_description && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      {result.shot_description.length > 120
                        ? result.shot_description.substring(0, 120) + "..."
                        : result.shot_description}
                    </Box>
                  )}
                  <ExpandableSection headerText="Details" variant="footer">
                    <SpaceBetween size="xs">
                      {result.shot_publicFigures && (
                        <div>
                          <Box variant="awsui-key-label">Public Figures</Box>
                          <Box>{result.shot_publicFigures}</Box>
                        </div>
                      )}
                      {result.shot_faces && (
                        <div>
                          <Box variant="awsui-key-label">Other Faces</Box>
                          <Box>{result.shot_faces}</Box>
                        </div>
                      )}
                      {result.shot_transcript && (
                        <div>
                          <Box variant="awsui-key-label">Transcript</Box>
                          <Box color="text-body-secondary">
                            <span style={{ whiteSpace: "pre-line" }}>{result.shot_transcript}</span>
                          </Box>
                        </div>
                      )}
                      {result.shot_description && (
                        <div>
                          <Box variant="awsui-key-label">Description</Box>
                          <Box color="text-body-secondary">
                            <span style={{ whiteSpace: "pre-line" }}>{result.shot_description}</span>
                          </Box>
                        </div>
                      )}
                    </SpaceBetween>
                  </ExpandableSection>
                </SpaceBetween>
              </CardBody>
            </ResultCard>
          );
        })}
      </ResultsGrid>
    </div>
  );
};

export default SearchResults;
