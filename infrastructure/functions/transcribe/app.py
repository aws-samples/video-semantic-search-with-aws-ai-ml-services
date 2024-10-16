import json
import boto3
from botocore.exceptions import ClientError
import os

transcribe_client = boto3.client("transcribe")
dynamodb_client = boto3.resource("dynamodb")


def lambda_handler(event, context):
    bucket_videos = os.environ["bucket_videos"]
    bucket_transcripts = os.environ["bucket_transcripts"]
    video_name = event["vssParams"]["video_name"]
    jobId = event["vssParams"]["jobId"]

    job = start_job(
        jobId,
        "s3://" + bucket_videos + "/" + video_name,
        "mp4",
        "en-US",
        transcribe_client,
        bucket_transcripts,
        None,
    )
    add_transcribe_taskid(
        jobId, os.environ["vss_dynamodb_table"], jobId, event["TaskToken"]
    )

    return {"statusCode": 200}


def start_job(
    job_name,
    media_uri,
    media_format,
    language_code,
    transcribe_client,
    output_bucket_name,
    vocabulary_name=None,
):
    """
    Starts a transcription job. This function returns as soon as the job is started.
    To get the current status of the job, call get_transcription_job. The job is
    successfully completed when the job status is 'COMPLETED'.

    :param job_name: The name of the transcription job. This must be unique for
                     your AWS account.
    :param media_uri: The URI where the audio file is stored. This is typically
                      in an Amazon S3 bucket.
    :param media_format: The format of the audio file. For example, mp3 or wav.
    :param language_code: The language code of the audio file.
                          For example, en-US or ja-JP
    :param transcribe_client: The Boto3 Transcribe client.
    :param vocabulary_name: The name of a custom vocabulary to use when transcribing
                            the audio file.
    :return: Data about the job.
    """
    job_args = {
        "TranscriptionJobName": job_name,
        "Media": {"MediaFileUri": media_uri},
        "MediaFormat": media_format,
        "LanguageCode": language_code,
        "Subtitles": {"Formats": ["srt"]},
        "OutputBucketName": output_bucket_name,
    }
    if vocabulary_name is not None:
        job_args["Settings"] = {"VocabularyName": vocabulary_name}
    response = transcribe_client.start_transcription_job(**job_args)
    job = response["TranscriptionJob"]
    return job


def add_transcribe_taskid(jobId, dynamodb_table, transcribe_job_id, sf_task_token):
    table = dynamodb_client.Table(dynamodb_table)
    dynamodbResponse = table.update_item(
        Key={"JobId": jobId},
        UpdateExpression="SET TranscribeTaskId = :value1, LambdaTranscribeTaskToken = :value2",
        ExpressionAttributeValues={
            ":value1": transcribe_job_id,
            ":value2": sf_task_token,
        },
    )
