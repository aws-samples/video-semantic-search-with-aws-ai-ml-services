// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState } from "react";
import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Select from "@cloudscape-design/components/select";
import Input from "@cloudscape-design/components/input";
import Toggle from "@cloudscape-design/components/toggle";
import ColumnLayout from "@cloudscape-design/components/column-layout";

export interface IndexingConfig {
  segmentationMode: "shot" | "interval";
  intervalSeconds: number;
  framesPerShot: number;
  transcription: boolean;
  faceRecognition: boolean;
  celebrityDetection: boolean;
}

interface IndexingConfigModalProps {
  visible: boolean;
  onDismiss: () => void;
  onConfirm: (config: IndexingConfig) => void;
}

const SEGMENTATION_OPTIONS = [
  { label: "Shot Detection", value: "shot" },
  { label: "Fixed Interval", value: "interval" },
];

const IndexingConfigModal: React.FC<IndexingConfigModalProps> = ({
  visible,
  onDismiss,
  onConfirm,
}) => {
  const [segmentationMode, setSegmentationMode] = useState<"shot" | "interval">(
    "shot"
  );
  const [intervalSeconds, setIntervalSeconds] = useState(10);
  const [framesPerShot, setFramesPerShot] = useState(3);
  const [transcription, setTranscription] = useState(true);
  const [faceRecognition, setFaceRecognition] = useState(true);
  const [celebrityDetection, setCelebrityDetection] = useState(true);

  const handleConfirm = () => {
    onConfirm({
      segmentationMode,
      intervalSeconds,
      framesPerShot,
      transcription,
      faceRecognition,
      celebrityDetection,
    });
  };

  const handleDismiss = () => {
    // Reset to defaults on dismiss
    setSegmentationMode("shot");
    setIntervalSeconds(10);
    setFramesPerShot(3);
    setTranscription(true);
    setFaceRecognition(true);
    setCelebrityDetection(true);
    onDismiss();
  };

  const selectedSegmentationOption =
    SEGMENTATION_OPTIONS.find((opt) => opt.value === segmentationMode) ||
    SEGMENTATION_OPTIONS[0];

  return (
    <Modal
      visible={visible}
      onDismiss={handleDismiss}
      header="Indexing Configuration"
      closeAriaLabel="Close"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={handleDismiss}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleConfirm}>
              Start Indexing
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        <FormField
          label="Segmentation Mode"
          description="How the video should be divided into segments for analysis"
        >
          <Select
            selectedOption={selectedSegmentationOption}
            onChange={({ detail }) => {
              setSegmentationMode(
                detail.selectedOption.value as "shot" | "interval"
              );
            }}
            options={SEGMENTATION_OPTIONS}
          />
        </FormField>

        {segmentationMode === "interval" && (
          <FormField
            label="Interval Seconds"
            description="Duration of each fixed segment in seconds"
          >
            <Input
              type="number"
              value={String(intervalSeconds)}
              onChange={({ detail }) => {
                const val = parseInt(detail.value, 10);
                if (!isNaN(val) && val > 0) {
                  setIntervalSeconds(val);
                }
              }}
            />
          </FormField>
        )}

        <FormField
          label="Frames Per Shot"
          description="Number of representative frames to extract from each segment"
        >
          <Input
            type="number"
            value={String(framesPerShot)}
            onChange={({ detail }) => {
              const val = parseInt(detail.value, 10);
              if (!isNaN(val) && val > 0) {
                setFramesPerShot(val);
              }
            }}
          />
        </FormField>

        <ColumnLayout columns={1}>
          <FormField label="Transcription">
            <Toggle
              checked={transcription}
              onChange={({ detail }) => setTranscription(detail.checked)}
            >
              Enable audio transcription
            </Toggle>
          </FormField>

          <FormField label="Face Recognition">
            <Toggle
              checked={faceRecognition}
              onChange={({ detail }) => setFaceRecognition(detail.checked)}
            >
              Enable face recognition for non-celebrity figures
            </Toggle>
          </FormField>

          <FormField label="Celebrity Detection">
            <Toggle
              checked={celebrityDetection}
              onChange={({ detail }) => setCelebrityDetection(detail.checked)}
            >
              Enable celebrity detection via Amazon Rekognition
            </Toggle>
          </FormField>
        </ColumnLayout>
      </SpaceBetween>
    </Modal>
  );
};

export default IndexingConfigModal;
