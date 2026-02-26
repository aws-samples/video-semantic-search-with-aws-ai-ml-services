// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Textarea from "@cloudscape-design/components/textarea";
import Input from "@cloudscape-design/components/input";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Modal from "@cloudscape-design/components/modal";
import styled from "styled-components";

export interface ShotCardProps {
  shotId: string;
  startTime: number;
  endTime: number;
  compositeUrl?: string;
  description: string;
  faces: { faceId?: string; label: string; isCelebrity: boolean; faceImageUrl?: string }[];
  transcript: string;
  onSave?: (description: string) => void;
  onSaveTranscript?: (transcript: string) => void;
  onLabelFace?: (faceId: string, label: string) => Promise<void>;
  onDeleteFace?: (faceId: string) => Promise<void>;
}

const CompositeImage = styled.img`
  width: 100%;
  max-height: 300px;
  object-fit: contain;
  border-radius: 4px;
  border: 1px solid #d5dbdb;
  background-color: #f0f0f0;
`;

const FaceCardWrapper = styled.div`
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 12px;
  border: 1px solid #d5dbdb;
  border-radius: 8px;
  background-color: #fafafa;
  min-width: 120px;
`;

const FaceDeleteWrapper = styled.div`
  position: absolute;
  top: 4px;
  right: 4px;
`;

const FaceImage = styled.img`
  width: 80px;
  height: 80px;
  border-radius: 50%;
  object-fit: cover;
  margin-bottom: 8px;
  border: 2px solid #d5dbdb;
  background-color: #e9ebed;
`;

const FacePlaceholder = styled.div`
  width: 80px;
  height: 80px;
  border-radius: 50%;
  background-color: #e9ebed;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 8px;
  font-size: 28px;
  color: #687078;
  border: 2px solid #d5dbdb;
`;

const FaceGrid = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
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

function isUnknownLabel(label: string): boolean {
  if (!label) return true;
  return label.toLowerCase().startsWith("unknown");
}

const ShotCard: React.FC<ShotCardProps> = ({
  shotId,
  startTime,
  endTime,
  compositeUrl,
  description,
  faces,
  transcript,
  onSave,
  onSaveTranscript,
  onLabelFace,
  onDeleteFace,
}) => {
  const [editableDescription, setEditableDescription] = useState(description);
  const [isEditing, setIsEditing] = useState(false);

  const [editableTranscript, setEditableTranscript] = useState(transcript);
  const [isEditingTranscript, setIsEditingTranscript] = useState(false);

  const [editingFaceId, setEditingFaceId] = useState<string | null>(null);
  const [faceEditValue, setFaceEditValue] = useState("");
  const [savingFaceId, setSavingFaceId] = useState<string | null>(null);
  const [deletingFaceId, setDeletingFaceId] = useState<string | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);

  const handleSave = () => {
    if (onSave) {
      onSave(editableDescription);
    }
    setIsEditing(false);
  };

  const handleSaveTranscript = () => {
    if (onSaveTranscript) {
      onSaveTranscript(editableTranscript);
    }
    setIsEditingTranscript(false);
  };

  const handleStartFaceEdit = (faceId: string, currentLabel: string) => {
    setEditingFaceId(faceId);
    setFaceEditValue(currentLabel);
  };

  const handleSaveFaceLabel = async (faceId: string) => {
    const trimmed = faceEditValue.trim();
    if (!trimmed || !onLabelFace) return;

    setEditingFaceId(null);
    setFaceEditValue("");
    setSavingFaceId(faceId);
    try {
      await onLabelFace(faceId, trimmed);
    } finally {
      setSavingFaceId(null);
    }
  };

  const handleCancelFaceEdit = () => {
    setEditingFaceId(null);
    setFaceEditValue("");
  };

  const handleConfirmDelete = async () => {
    if (!deletingFaceId || !onDeleteFace) return;
    setDeleteLoading(true);
    try {
      await onDeleteFace(deletingFaceId);
    } finally {
      setDeleteLoading(false);
      setDeletingFaceId(null);
    }
  };

  const renderFaceAvatar = (face: { faceImageUrl?: string; label: string }) => {
    if (face.faceImageUrl) {
      return <FaceImage src={face.faceImageUrl} alt={face.label || "Face"} />;
    }
    return (
      <FacePlaceholder>
        {isUnknownLabel(face.label) ? "?" : face.label.charAt(0).toUpperCase()}
      </FacePlaceholder>
    );
  };

  return (
    <Container
      header={
        <Header
          variant="h3"
          description={`${millisecondsToTimeFormat(startTime)} - ${millisecondsToTimeFormat(endTime)}`}
        >
          Shot {shotId}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {/* Composite tile image */}
        {compositeUrl ? (
          <CompositeImage
            src={compositeUrl}
            alt={`Composite tile for ${shotId}`}
          />
        ) : (
          <Box
            textAlign="center"
            padding="l"
            color="text-status-inactive"
          >
            No composite image available.
          </Box>
        )}

        {/* Description */}
        <ExpandableSection headerText="Description" defaultExpanded>
          {isEditing ? (
            <SpaceBetween size="xs">
              <Textarea
                value={editableDescription}
                onChange={({ detail }) =>
                  setEditableDescription(detail.value)
                }
                rows={4}
              />
              <SpaceBetween size="xs" direction="horizontal">
                <Button variant="primary" onClick={handleSave}>
                  Save
                </Button>
                <Button
                  variant="link"
                  onClick={() => {
                    setEditableDescription(description);
                    setIsEditing(false);
                  }}
                >
                  Cancel
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          ) : (
            <SpaceBetween size="xs">
              <Box variant="p">
                {description || "No description available."}
              </Box>
              {onSave && (
                <Button
                  variant="inline-link"
                  iconName="edit"
                  onClick={() => {
                    setEditableDescription(description);
                    setIsEditing(true);
                  }}
                >
                  Edit
                </Button>
              )}
            </SpaceBetween>
          )}
        </ExpandableSection>

        {/* Faces */}
        <ExpandableSection headerText="Faces" defaultExpanded>
          {faces.length > 0 ? (
            <FaceGrid>
              {faces.map((face, index) => {
                const faceKey = face.faceId || `face-${index}`;
                return (
                  <FaceCardWrapper key={faceKey}>
                    {onDeleteFace && face.faceId && (
                      <FaceDeleteWrapper>
                        <Button
                          iconName="close"
                          variant="icon"
                          ariaLabel={`Delete face ${face.label || face.faceId}`}
                          onClick={() => setDeletingFaceId(face.faceId!)}
                        />
                      </FaceDeleteWrapper>
                    )}
                    {renderFaceAvatar(face)}
                    {editingFaceId === face.faceId ? (
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
                        <Input
                          value={faceEditValue}
                          onChange={({ detail }) => setFaceEditValue(detail.value)}
                          placeholder="Enter name"
                          onKeyDown={(event) => {
                            if (event.detail.key === "Enter") {
                              handleSaveFaceLabel(face.faceId!);
                            }
                          }}
                        />
                        <Button
                          variant="primary"
                          onClick={() => handleSaveFaceLabel(face.faceId!)}
                        >
                          Save
                        </Button>
                        <Button variant="link" onClick={handleCancelFaceEdit}>
                          Cancel
                        </Button>
                      </div>
                    ) : savingFaceId === face.faceId ? (
                      <StatusIndicator type="loading">Saving...</StatusIndicator>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
                        <StatusIndicator
                          type={face.isCelebrity ? "success" : "warning"}
                        >
                          {face.label || "Unknown"}
                        </StatusIndicator>
                        {onLabelFace && face.faceId && (
                          <Button
                            variant="inline-link"
                            onClick={() => handleStartFaceEdit(face.faceId!, face.label)}
                          >
                            Assign Name
                          </Button>
                        )}
                      </div>
                    )}
                  </FaceCardWrapper>
                );
              })}
            </FaceGrid>
          ) : (
            <Box color="text-status-inactive">No faces detected</Box>
          )}
        </ExpandableSection>

        {/* Transcript */}
        <ExpandableSection headerText="Transcript" defaultExpanded>
          {isEditingTranscript ? (
            <SpaceBetween size="xs">
              <Textarea
                value={editableTranscript}
                onChange={({ detail }) =>
                  setEditableTranscript(detail.value)
                }
                rows={4}
              />
              <SpaceBetween size="xs" direction="horizontal">
                <Button variant="primary" onClick={handleSaveTranscript}>
                  Save
                </Button>
                <Button
                  variant="link"
                  onClick={() => {
                    setEditableTranscript(transcript);
                    setIsEditingTranscript(false);
                  }}
                >
                  Cancel
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          ) : (
            <SpaceBetween size="xs">
              <Box variant="p" color={transcript ? "text-body-secondary" : "text-status-inactive"}>
                {transcript || "No transcript available."}
              </Box>
              {onSaveTranscript && (
                <Button
                  variant="inline-link"
                  iconName="edit"
                  onClick={() => {
                    setEditableTranscript(transcript);
                    setIsEditingTranscript(true);
                  }}
                >
                  Edit
                </Button>
              )}
            </SpaceBetween>
          )}
        </ExpandableSection>
      </SpaceBetween>

      <Modal
        visible={deletingFaceId !== null}
        onDismiss={() => setDeletingFaceId(null)}
        header="Delete face"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setDeletingFaceId(null)}
                disabled={deleteLoading}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleConfirmDelete}
                loading={deleteLoading}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Are you sure you want to permanently delete this face? This will remove
        it from the graph database and the face collection. This action cannot
        be undone.
      </Modal>
    </Container>
  );
};

export default ShotCard;
