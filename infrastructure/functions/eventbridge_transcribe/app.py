import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
import os
import json
import re
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

bedrock_client = boto3.client(service_name="bedrock-runtime")
dynamodb_client = boto3.resource("dynamodb")
s3_client = boto3.client("s3")


def lambda_handler(event, context):
    dynamodb_table = os.environ["vss_dynamodb_table"]
    table = dynamodb_client.Table(dynamodb_table)

    transcribeTaskId = event["detail"]["TranscriptionJobName"]

    response = table.query(
        IndexName="TranscribeGSI",
        KeyConditionExpression=Key("TranscribeTaskId").eq(transcribeTaskId),
    )
    item = response["Items"][0]
    jobId = item["JobId"]

    subtitle = get_subtitle(os.environ["bucket_transcripts"], jobId + ".srt")
    processed_transcript = process_transcript(subtitle)
    s3_client.put_object(
        Body=json.dumps(processed_transcript).encode("utf-8"),
        Bucket=os.environ["bucket_transcripts"],
        Key=f"{jobId}.json",
        ContentType="application/json",
    )

    client = get_opensearch_client(os.environ["aoss_host"], os.environ["region"])

    for sentence in processed_transcript:
        aoss_request_body = json.dumps(
        {
            "jobId": jobId,
            "video_name": item["Input"],
            "transcript_id": f"{sentence["sentence_startTime"] - sentence["sentence_endTime"]}",
            "transcript_startTime": sentence["sentence_startTime"],
            "transcript_endTime": sentence["sentence_endTime"],
            "transcript": sentence["sentence"],
            "transcript_vector": get_text_embedding(os.environ["text_embedding_model"], sentence["sentence"])
        }
    )
        response = client.index(
            index=os.environ["aoss_audio_index"],
            body=aoss_request_body,
            params={"timeout": 60},
        )

    sfTaskToken = item["LambdaTranscribeTaskToken"]

    # sendTaskSuccess to Step Function to notify Transcribe has successfully finished the job
    stepfunctions = boto3.client("stepfunctions")
    sfResponse = stepfunctions.send_task_success(taskToken=sfTaskToken, output="{}")

    return {"statusCode": 200}


def get_subtitle(bucket_transcripts, transcript_filename):
    try:
        subtitle = (
            s3_client.get_object(Bucket=bucket_transcripts, Key=transcript_filename)["Body"]
            .read()
            .decode("utf-8-sig")
        )
        return subtitle
    except Exception as e:
        return "" 


def process_transcript(s):
    if s == "":
        return []
    subtitle_blocks = re.findall(
        r"(\d+\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\d+\n|\Z))",
        s,
        re.DOTALL,
    )

    sentences = [block[3].replace("\n", " ").strip() for block in subtitle_blocks]
    startTimes = [block[1] for block in subtitle_blocks]
    endTimes = [block[2] for block in subtitle_blocks]
    startTimes_ms = [time_to_ms(time) for time in startTimes]
    endTimes_ms = [time_to_ms(time) for time in endTimes]

    filtered_sentences = []
    filtered_startTimes_ms = []
    filtered_endTimes_ms = []

    startTime_ms = -1
    endTime_ms = -1
    sentence = ""
    for i in range(len(sentences)):
        if startTime_ms == -1:
            startTime_ms = startTimes_ms[i]
        sentence += " " + sentences[i]
        if (
            sentences[i].endswith(".")
            or sentences[i].endswith("?")
            or sentences[i].endswith("!")
            or i == len(sentences) - 1
        ):
            endTime_ms = endTimes_ms[i]
            filtered_sentences.append(sentence.strip())
            filtered_startTimes_ms.append(startTime_ms)
            filtered_endTimes_ms.append(endTime_ms)
            startTime_ms = -1
            endTime_ms = -1
            sentence = ""

    processed_transcript = []
    for i in range(len(filtered_sentences)):
        processed_transcript.append(
            {
                "sentence_startTime": filtered_startTimes_ms[i],
                "sentence_endTime": filtered_endTimes_ms[i],
                "sentence": filtered_sentences[i],
            }
        )

    return processed_transcript


def time_to_ms(time_str):
    h, m, s, ms = re.split(":|,", time_str)
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

def get_opensearch_client(host, region):
    host = host.split("://")[1] if "://" in host else host
    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")

    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )

    return client

def get_text_embedding(text_embedding_model, text):
    accept = "application/json"
    content_type = "application/json"
    if text_embedding_model.startswith("amazon.titan-embed-text"):
        body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})
        response = bedrock_client.invoke_model(
            body=body,
            modelId=text_embedding_model,
            accept=accept,
            contentType=content_type,
        )
        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding")
    else:
        if len(text) > 2048:
            text = text[:2048]
        body = json.dumps({"texts": [text], "input_type": "search_document"})
        response = bedrock_client.invoke_model(
            body=body,
            modelId=text_embedding_model,
            accept=accept,
            contentType=content_type,
        )
        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embeddings")[0]

    return embedding
