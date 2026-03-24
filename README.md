# Video Semantic Search

Media companies, content creators, and video archivists can have terabytes or even petabytes of video footage, making it difficult to sort and find content.

Video Semantic Search leverages [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/), [Amazon Rekognition](https://aws.amazon.com/rekognition/), [Amazon Transcribe](https://aws.amazon.com/transcribe/), [Amazon Neptune Analytics](https://aws.amazon.com/neptune/features/neptune-analytics/) and [Amazon OpenSearch](https://aws.amazon.com/opensearch-service/features/serverless/) to enable quick and efficient searching for specific scenes, actions, concepts, people, or objects within large volumes of video data using natural language queries.

By harnessing the power of semantic understanding and multimodal analysis, users can formulate intuitive queries and receive relevant results, significantly enhancing the discoverability and usability of extensive video libraries. This in turn enables rapid footage retrieval, and unlocks new creative possibilities.

## Solution overview

![Architecture diagram - Video Semantic Search](assets/video-semantic-search-architecture.png?raw=true "Architecture diagram - Video Semantic Search")

These following steps walk through the sequence of actions that enable video semantic search with AWS AI/ML services.

1. [Amazon Simple Storage Service (Amazon S3)](https://aws.amazon.com/s3/) hosts a static website for the video semantic search, served by an [Amazon CloudFront](https://aws.amazon.com/cloudfront/) distribution. [Amazon Cognito](https://aws.amazon.com/cognito/) provides customer identity and access management for the web application.
2. Upload videos to [Amazon S3](https://aws.amazon.com/s3/) with [S3 pre-signed URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ShareObjectPreSignedURL.html).
3. After a video is uploaded successfully, an API call to [Amazon API Gateway](https://aws.amazon.com/api-gateway/) triggers [AWS Lambda](https://aws.amazon.com/lambda/) to queue new indexing-video request in [Amazon Simple Queue Service (Amazon SQS)](https://aws.amazon.com/sqs/).
4. AWS Lambda processes new messages in the SQS queue, initiating [AWS Step Functions](https://aws.amazon.com/step-functions/) workflow.
5. [Amazon Rekognition](https://docs.aws.amazon.com/rekognition/latest/dg/segments.html) detects multiple video shots from the original video, containing the start, end, and duration of each shot. Alternatively, interval-based segmentation can be used to split videos into fixed-duration segments. Shot/segment metadata is used to generate sequence of frames which are grouped by individual video shot and stored in Amazon S3.
6. In parallel, create an [Amazon Transcribe](https://aws.amazon.com/transcribe/) job to generate a transcription for the video.
7. AWS Step Functions uses the [Map state](https://docs.aws.amazon.com/step-functions/latest/dg/state-map.html) to run a set of workflow for each video shot stored in Amazon S3 in parallel.
8. [Amazon Rekognition](https://docs.aws.amazon.com/rekognition/latest/dg/celebrities.html) detects celebrities in the shots. [Amazon Rekognition Face Collection](https://docs.aws.amazon.com/rekognition/latest/dg/collections.html) indexes detected faces and matches them across shots for consistent person tracking. Face recognition data and person-segment relationships are stored in [Amazon Neptune Analytics](https://aws.amazon.com/neptune/features/neptune-analytics/) as a knowledge graph.
9. Foundation model in Amazon Bedrock generates shots’ contextual descriptions from shots’ visual images as well as relevant audio transcriptions.
10. [Amazon Nova Multimodal Embeddings](https://aws.amazon.com/ai/generative-ai/nova/) model in Amazon Bedrock generates text, image, and video embeddings of video shots’ descriptions, visual frames, and video clips. [Amazon OpenSearch](https://aws.amazon.com/opensearch-service/features/serverless/) stores the embeddings and other shots’ metadata in vector database. [Amazon Neptune Analytics](https://aws.amazon.com/neptune/features/neptune-analytics/) stores the video structure as a knowledge graph including segments, frames, face appearances, and temporal relationships.
11. Embedding model in Amazon Bedrock generates the embedding of the users’ query which is then used to perform semantic search for the videos from Amazon OpenSearch vector database. Combine semantic search with traditional keyword searches across other fields to enhance search accuracy. [Cohere Rerank](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank.html) model reranks results for improved relevance. Optionally, [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) provides an agentic RAG search strategy that uses an AI agent (built with [Strands Agents](https://strandsagents.com/)) to intelligently reason about the query, perform person-aware searches via the Neptune knowledge graph, and deliver more accurate results.
12. [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) tables store profiling and video indexing job metadata to keep track of the jobs’ status and other relevant information. [Amazon Neptune Analytics](https://aws.amazon.com/neptune/features/neptune-analytics/) graph database stores face recognition data, person identities, and video-person relationships.

For further information, please refer to the links below:

**AWS at NAB 2025:** [Video Semantic Search Demo at NAB 2025](https://aws.amazon.com/media/nab25-demos/video-semantic-search/)

**AWS at IBC 2024:** [Video Semantic Search Demo at IBC 2024](https://aws.amazon.com/media/ibc24-demos/data-science-and-analytics-video-semantic-search/)

**AWS Solution Library:** [Guidance for Semantic Video Search on AWS](https://aws.amazon.com/solutions/guidance/semantic-video-search-on-aws/)

## Pre-requisites

- SAM CLI

  The solution uses [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) to provision and manage infrastructure.

- Node

  The front end for this solution is a React/TypeScript web application that can be run locally using Node

- npm

  The installation of the packages required to run the web application locally, or build it for remote deployment, require npm.

- Docker

  This solution has been built and tested using SAM CLI in conjunction with [Docker Desktop](https://www.docker.com/products/docker-desktop/) to make the build process as smooth as possible. It is possible to build and deploy this solution without Docker but we recommend using Docker as it reduces the number of local dependancies needed.
  Note that the deploy script described below requires Docker to be installed.

## Amazon Bedrock requirements

**Base Models Access**

If you are looking to interact with models from Amazon Bedrock, you need to [request access to the base models in one of the regions where Amazon Bedrock is available](https://console.aws.amazon.com/bedrock/home?#/modelaccess). Make sure to read and accept models' end-user license agreements or EULA.

Note:

- You can deploy the solution to a different region from where you requested Base Model access.
- While the Base Model access approval is instant, it might take several minutes to get access and see the list of models in the console.
- The current deployment requires access to **Claude Sonnet 4.6**, **Claude Haiku 4.5**, **Amazon Nova Multimodal Embeddings v1**, and **Cohere Rerank 3.5**.

## Deployment

This deployment is currently set up to deploy into the **us-east-1** region. Please check Amazon Bedrock region availability and update the `infrastructure/samconfig.toml` file to reflect your desired region.

### Environment setup

1. Clone the repository

```bash
git clone https://github.com/aws-samples/video-semantic-search-with-aws-ai-ml-services.git
```

2. Move into the cloned repository

```bash
cd video-semantic-search-with-aws-ai-ml-services
```

3. Start the deployment

> [!IMPORTANT]
> Ensure that Docker is installed and running before proceeding with the deployment.

```bash
./scripts/deploy.sh
```

The deploy script supports the following options:

```bash
./scripts/deploy.sh                  # Deploy all (backend + frontend)
./scripts/deploy.sh --backend        # Deploy backend only (SAM stack + AgentCore)
./scripts/deploy.sh --frontend       # Deploy frontend only
```

The deployment can take approximately 10-15 minutes for a full deployment.

### Create login details for the web application

The authenication is managed by Amazon Cognito. You will need to create a new user to be able to login.

### Login to your new web application

Once complete, the CLI output will show a value for the CloudFront URL to be able to view the web application, e.g. `https://d123abc.cloudfront.net/`

## User Experience

The solution currently supports the following features:

### Search

1. **Text Search**

- Input text-based query to search for specific content. Example: All the scenes that feature football field.
- Use quotation marks ("") to emphasize specific keywords. Example: Werner Vogels "shaking hands" with other people.
- **Agentic Search** toggle enables an AI agent (powered by Amazon Bedrock AgentCore) that intelligently reasons about queries, performs person-aware searches using the knowledge graph, and reranks results for improved accuracy.

2. **Image Search**

- Upload an image to find similar or related content.

3. **Clip Search**

- Upload a video clip to find the original source video that contains the clip.

### Admin Portal

- View and manage all video indexing jobs with status tracking and progress monitoring.
- Configure indexing settings (segmentation method, model parameters) per job.
- Track processing costs in real-time.
- **Face Gallery** — browse detected faces across indexed videos, label and manage face identities stored in the Neptune knowledge graph.

![UI](assets/video-semantic-search-ui.gif "Video Semantic Search UI")

## Troubleshooting

If you encounter any issues during video indexing process, please consider the following steps:

1. **Step Function Workflow:** Check the Step Function workflow for detailed execution logs and error messages. This is often the best starting point for debugging.

2. **Bedrock Access:** Ensure that you have the necessary permissions and access to use Amazon Bedrock in your AWS account and region.

3. **Bedrock Quotas:** Verify your Amazon Bedrock RPM (Requests Per Minute) and TPM (Tokens Per Minute) quotas for the specific models used in the solution. Insufficient quotas can lead to throttling or failures.

4. **Model Availability:** Confirm that the foundation models required for this solution are available in your AWS region.

## Clean Up

Run the destroy script to remove all resources created by this solution:

```bash
./scripts/destroy.sh
```

The script will prompt for confirmation before proceeding. It performs the following cleanup:

1. Empties all S3 buckets (videos, shots, images, transcripts, clip search, static web, logging, agent code)
2. Deletes the Rekognition face collection
3. Deletes the AgentCore CloudFormation stack
4. Deletes the main CloudFormation stack (including Neptune graph, OpenSearch collection, and all other resources)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
