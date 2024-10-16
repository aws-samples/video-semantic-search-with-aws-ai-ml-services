# Video Semantic Search

Media companies, content creators, and video archivists can have terabytes or even petabytes of video footage, making it difficult to sort and find content.

Video Semantic Search leverages [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Amazon Rekognition](https://aws.amazon.com/rekognition/), [Amazon Transcribe](https://aws.amazon.com/transcribe/) and [Amazon OpenSearch](https://aws.amazon.com/opensearch-service/features/serverless/) to enable quick and efficient searching for specific scenes, actions, concepts, people, or objects within large volumes of video data using natural language queries.

By harnessing the power of semantic understanding and multimodal analysis, users can formulate intuitive queries and receive relevant results, significantly enhancing the discoverability and usability of extensive video libraries. This in turn enables rapid footage retrieval, and unlocks new creative possibilities.

## Solution overview

![Architecture diagram - Video Semantic Search](assets/video-semantic-search-architecture.png?raw=true "Architecture diagram - Video Semantic Search")

These following steps walk through the sequence of actions that enable video semantic search with AWS AI/ML services.

1. [Amazon Simple Storage Service (Amazon S3)](https://aws.amazon.com/s3/) hosts a static website for the video semantic search, served by an [Amazon CloudFront](https://aws.amazon.com/cloudfront/) distribution. [Amazon Cognito](https://aws.amazon.com/cognito/) provides customer identity and access management for the web application.
2. Upload videos to [Amazon S3](https://aws.amazon.com/s3/) with [S3 pre-signed URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ShareObjectPreSignedURL.html).
3. After a video is uploaded successfully, an API call to [Amazon API Gateway](https://aws.amazon.com/api-gateway/) triggers [AWS Lambda](https://aws.amazon.com/lambda/) to queue new indexing-video request in [Amazon Simple Queue Service (Amazon SQS)](https://aws.amazon.com/sqs/).
4. AWS Lambda processes new messages in the SQS queue, initiating [AWS Step Functions](https://aws.amazon.com/step-functions/) workflow.
5. [Amazon Rekognition](https://docs.aws.amazon.com/rekognition/latest/dg/segments.html) detect multiple video shots from the original video, containing the start, end, and duration of each shot. Shot metadata is used to generate sequence of frames which are grouped by individual video shot and stored in Amazon S3.
6. In parallel, create an [Amazon Transcribe](https://aws.amazon.com/transcribe/) job to generate a transcription for the video.
7. AWS Step Functions uses the [Map state](https://docs.aws.amazon.com/step-functions/latest/dg/state-map.html) to run a set of workflow for each video shot stored in Amazon S3 in parallel.
8. [Amazon Rekognition](https://docs.aws.amazon.com/rekognition/latest/dg/celebrities.html) detects celebrities in the shots. Foundation model in [Amazon Bedrock](https://aws.amazon.com/bedrock/) detects private figures by analyzing text labels or titles that appear in the video shots. Use multimodal LLMs in Amazon Bedrock to generate image embeddings and compare shot similarities, propagating recognized figures across visually similar shots even when faces are obscured or titles are absent.
9. Foundation model in Amazon Bedrock generates shots’ contextual descriptions from shots’ visual images, detected celebrities and private figures as well as relevant audio transcriptions.
10. Embedding model in Amazon Bedrock generates the embeddings of video shots’ descriptions and visual images. [Amazon OpenSearch](https://aws.amazon.com/opensearch-service/features/serverless/) stores the embeddings and other shots’ metadata in vector database.
11. Embedding model in Amazon Bedrock generates the embedding of the users’ query which is then used to perform semantic search for the videos from Amazon OpenSearch vector database. Combine semantic search with traditional keyword searches across other fields to enhance search accuracy.
12. [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) tables store profiling and video indexing job metadata to keep track of the jobs’ status and other relevant information

For further information, please refer to the links below:

**AWS at IBC 2024:** [Video Semantic Search Demo at IBC 2024](https://aws.amazon.com/media/ibc24-demos/data-science-and-analytics-video-semantic-search/)

**AWS at NAB 2024:** [Video Semantic Search Demo at NAB 2024](https://aws.amazon.com/media/nab24-demos/nab24-demos-data-science-and-analytics-video-semantic-search/)

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
  Note that the npm `deploy` script described below requires Docker to be installed.

## Amazon Bedrock requirements

**Base Models Access**

If you are looking to interact with models from Amazon Bedrock, you need to [request access to the base models in one of the regions where Amazon Bedrock is available](https://console.aws.amazon.com/bedrock/home?#/modelaccess). Make sure to read and accept models' end-user license agreements or EULA.

Note:

- You can deploy the solution to a different region from where you requested Base Model access.
- While the Base Model access approval is instant, it might take several minutes to get access and see the list of models in the console.
- The current deployment requires access to **Claude 3 Sonnet** and **Claude 3.5 Sonnet**.

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

```bash
cd frontend
npm install
npm run deploy
```

The deployment can take approximately 5-10 minutes.

### Create login details for the web application

The authenication is managed by Amazon Cognito. You will need to create a new user to be able to login.

### Login to your new web application

Once complete, the CLI output will show a value for the CloudFront URL to be able to view the web application, e.g. https://d123abc.cloudfront.net/

### User Interface

![UI](assets/video-semantic-search-ui.gif "Video Semantic Search UI")

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
