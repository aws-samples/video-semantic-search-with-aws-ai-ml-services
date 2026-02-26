// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import styled from "styled-components";

export interface Face {
  faceId: string;
  label: string;
  isCelebrity: boolean;
  imageUrl?: string;
}

interface FaceGalleryProps {
  faces: Face[];
  onLabelFace: (faceId: string, label: string) => Promise<void>;
  onDeleteFace: (faceId: string) => Promise<void>;
}

const FaceCard = styled.div`
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 12px;
  border: 1px solid #d5dbdb;
  border-radius: 8px;
  background-color: #fafafa;
  min-width: 140px;
`;

const DeleteButtonWrapper = styled.div`
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
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
  max-height: 400px;
  overflow-y: auto;
  padding: 4px;
`;

function isUnknownLabel(label: string): boolean {
  if (!label) return true;
  return label.toLowerCase().startsWith("unknown");
}

const FaceGallery: React.FC<FaceGalleryProps> = ({ faces, onLabelFace, onDeleteFace }) => {
  const [editingFaceId, setEditingFaceId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [savingFaceId, setSavingFaceId] = useState<string | null>(null);
  const [pendingLabel, setPendingLabel] = useState<string>("");
  const [deletingFaceId, setDeletingFaceId] = useState<string | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);

  const handleStartEdit = (face: Face) => {
    setEditingFaceId(face.faceId);
    setEditValue(face.label);
  };

  const handleSaveLabel = async (faceId: string) => {
    const trimmed = editValue.trim();
    if (!trimmed) return;

    setEditingFaceId(null);
    setEditValue("");
    setSavingFaceId(faceId);
    setPendingLabel(trimmed);

    try {
      await onLabelFace(faceId, trimmed);
    } finally {
      setSavingFaceId(null);
      setPendingLabel("");
    }
  };

  const handleCancelEdit = () => {
    setEditingFaceId(null);
    setEditValue("");
  };

  const handleConfirmDelete = async () => {
    if (!deletingFaceId) return;
    setDeleteLoading(true);
    try {
      await onDeleteFace(deletingFaceId);
    } finally {
      setDeleteLoading(false);
      setDeletingFaceId(null);
    }
  };

  const identified = faces.filter((f) => !isUnknownLabel(f.label));
  const unknowns = faces.filter((f) => isUnknownLabel(f.label));

  const renderFaceAvatar = (face: Face) => {
    if (face.imageUrl) {
      return <FaceImage src={face.imageUrl} alt={face.label || "Face"} />;
    }
    return (
      <FacePlaceholder>
        {isUnknownLabel(face.label) ? "?" : face.label.charAt(0).toUpperCase()}
      </FacePlaceholder>
    );
  };

  const renderFaceCard = (face: Face) => (
    <FaceCard key={face.faceId || face.label}>
      <DeleteButtonWrapper>
        <Button
          iconName="close"
          variant="icon"
          ariaLabel={`Delete face ${face.label || face.faceId}`}
          onClick={() => setDeletingFaceId(face.faceId)}
        />
      </DeleteButtonWrapper>
      {renderFaceAvatar(face)}
      {editingFaceId === face.faceId ? (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
          <Input
            value={editValue}
            onChange={({ detail }) => setEditValue(detail.value)}
            placeholder="Enter name"
            onKeyDown={(event) => {
              if (event.detail.key === "Enter") {
                handleSaveLabel(face.faceId);
              }
            }}
          />
          <Button
            variant="primary"
            onClick={() => handleSaveLabel(face.faceId)}
          >
            Save
          </Button>
          <Button variant="link" onClick={handleCancelEdit}>
            Cancel
          </Button>
        </div>
      ) : savingFaceId === face.faceId ? (
        <StatusIndicator type="loading">{pendingLabel}</StatusIndicator>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
          <StatusIndicator
            type={isUnknownLabel(face.label) ? "warning" : "success"}
          >
            {face.label || "Unknown"}
          </StatusIndicator>
          <Button variant="inline-link" onClick={() => handleStartEdit(face)}>
            Assign Name
          </Button>
        </div>
      )}
    </FaceCard>
  );

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="All faces detected across the video"
          counter={`(${faces.length})`}
        >
          Face Gallery
        </Header>
      }
    >
      <SpaceBetween size="l">
        {identified.length > 0 && (
          <SpaceBetween size="s">
            <Box variant="h3">Identified Persons ({identified.length})</Box>
            <FaceGrid>{identified.map(renderFaceCard)}</FaceGrid>
          </SpaceBetween>
        )}

        {unknowns.length > 0 && (
          <SpaceBetween size="s">
            <Box variant="h3">Unknown Persons ({unknowns.length})</Box>
            <FaceGrid>{unknowns.map(renderFaceCard)}</FaceGrid>
          </SpaceBetween>
        )}

        {faces.length === 0 && (
          <Box textAlign="center" color="inherit" padding="l">
            <b>No faces detected</b>
            <Box variant="p" color="inherit">
              Face detection did not identify any faces in this video.
            </Box>
          </Box>
        )}
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

export default FaceGallery;
