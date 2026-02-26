# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Video Semantic Search — an AWS serverless solution that lets users search video libraries using natural language, images, or video clips. Videos are automatically analyzed using AI/ML services and indexed into a vector database for semantic retrieval.

## Common Commands

### Frontend (from `frontend/`)

```bash
npm install          # Install dependencies
npm run dev          # Start local dev server (Vite, port 8080)
npm run build        # TypeScript check + Vite production build
npm run lint         # ESLint (ts, tsx, js files)
npm run config       # Generate .env.local from deployed CloudFormation stack outputs
npm run deploy       # Full deploy: sam build → sam deploy → config → build → S3 upload
```

### Infrastructure (from `infrastructure/`)

```bash
sam build --use-container                    # Build Lambda functions (Docker required)
sam deploy                                   # Deploy stack (uses samconfig.toml defaults)
sam validate --lint                           # Validate/lint CloudFormation template
sam delete                                   # Tear down the stack
```

The SAM stack name is `vss`, default region is `us-east-1` (configured in `infrastructure/samconfig.toml`).

## Architecture

### Frontend

Single-page React 18 app using Cloudscape Design System and AWS Amplify for Cognito auth. The entire UI lives in `frontend/src/pages/home.tsx` — a single page handling video upload, job status display, and search (text/image/clip). Runtime config is injected via Vite env vars (`VITE_APP_*`) generated from CloudFormation outputs by `frontend/cli.js`.

### Infrastructure

All AWS resources are defined in `infrastructure/template.yaml` (SAM/CloudFormation). Key resources: API Gateway (HttpApi), Cognito User Pool, S3 buckets, DynamoDB tables, SQS queue, Step Functions state machine, OpenSearch Serverless collection, and 17 Lambda functions.

### Video Indexing Pipeline (Step Functions)

Defined in `infrastructure/step_function.json`. The workflow processes an uploaded video through these stages:

1. **Parallel stage** — Runs simultaneously:
   - `transcribe` → starts Amazon Transcribe job (uses waitForTaskToken callback)
   - `rekognition_shot_detection` → detects shot boundaries via Rekognition (uses waitForTaskToken callback; SNS notification handled by `rekognition_shot_detection_sns`)
2. **Video Shots Map (distributed, max 10 concurrent)** — For each detected shot:
   - `generate_shot_image` → extracts representative frames using ffmpeg
   - Then in parallel: `rekognition_celebrity_detection` + `rekognize_other_figures` (Bedrock LLM detects non-celebrity figures from text/titles in frames)
   - `create_shot_collection` → consolidates shot metadata (figures, frames) into S3 JSON
3. **Video Shot (2) Map (distributed, max 10 concurrent)** — For each shot:
   - `generate_shot_desc` → Bedrock LLM generates contextual description from frames + figures + transcript
   - `embedding_aoss` → generates embeddings (text via Cohere, image via Titan) and indexes into OpenSearch Serverless
4. **Terminal** — `completedjob` or `failedjob` updates DynamoDB status

### Search (Lambda: `search`)

Supports three query types:
- **Text** — Generates Cohere text embedding, runs hybrid kNN search on `shot_desc_vector` (75% weight) + `shot_transcript_vector` (25% weight) in OpenSearch, then reranks with Cohere Rerank 3.5 (us-west-2 only)
- **Image** — Generates Titan image embedding, runs kNN on `shot_image_vector`
- **Clip** — Extracts frames via ffmpeg, runs image search per frame, aggregates scores across frames to find the source video

### Lambda Functions

All in `infrastructure/functions/`, Python 3.12, each with `app.py` entry point (`lambda_handler`). Shared dependency layer for `opensearchpy` in `infrastructure/layers/opensearch/`. The `generate_shot_image` and `search` functions use an ffmpeg binary from `infrastructure/layers/ffmpeg/`.

### Key AWS Service Dependencies

- **Amazon Bedrock models**: Claude 3.7 Sonnet (description generation), Claude Sonnet 4 / Nova Pro (figure detection), Cohere Embed English v3 (text embeddings, 1024d), Titan Multimodal Embeddings G1 (image embeddings, 1024d), Cohere Rerank 3.5
- **OpenSearch Serverless**: Vector search collection with index `vss-visual-index` (shots) and `vss-audio-index` (transcripts)
- **DynamoDB**: Job profiling and status tracking
- **S3 buckets**: Video uploads, shot frames, clip search uploads, frontend hosting

## Conventions

- Frontend uses Cloudscape Design System components — follow existing patterns in `home.tsx` for UI changes
- Lambda functions are standalone Python modules; each gets its own IAM role defined in `template.yaml`
- Environment variables for Lambda functions are configured in `template.yaml` resource definitions, not in `.env` files
- The frontend `.env.local` is auto-generated from CloudFormation stack outputs — do not manually edit it
- Async Rekognition/Transcribe jobs use Step Functions waitForTaskToken pattern with SNS/EventBridge callbacks
