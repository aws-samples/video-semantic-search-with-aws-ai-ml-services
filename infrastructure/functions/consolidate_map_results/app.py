import json
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def lambda_handler(event, context):
    """
    Reads a Step Functions ResultWriter manifest, collects all SUCCEEDED
    results, and writes them as a single JSON array to S3 for downstream
    ItemReader (InputType: JSON).

    ResultWriter SUCCEEDED files are JSON arrays where each element has
    an "Output" field that is a JSON-encoded string. This function parses
    that string so the consolidated file contains plain JSON objects.
    """
    result_writer_details = event["resultWriterDetails"]
    bucket = result_writer_details["Bucket"]
    manifest_key = result_writer_details["Key"]

    # Read the ResultWriter manifest
    response = s3.get_object(Bucket=bucket, Key=manifest_key)
    manifest = json.loads(response["Body"].read().decode("utf-8"))

    dest_bucket = manifest.get("DestinationBucket", bucket)

    # Read all SUCCEEDED result files and extract/parse Output from each record
    items = []
    for file_info in manifest.get("ResultFiles", {}).get("SUCCEEDED", []):
        file_response = s3.get_object(Bucket=dest_bucket, Key=file_info["Key"])
        content = file_response["Body"].read().decode("utf-8")
        records = json.loads(content)
        for record in records:
            output = record.get("Output", record)
            # Output is a JSON-encoded string in ResultWriter files
            if isinstance(output, str):
                output = json.loads(output)
            items.append(output)

    logger.info(f"Consolidated {len(items)} items from ResultWriter manifest")

    # Write consolidated JSON array alongside the manifest
    output_key = manifest_key.rsplit("/", 1)[0] + "/consolidated.json"
    s3.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=json.dumps(items),
        ContentType="application/json",
    )

    return {"consolidatedBucket": bucket, "consolidatedKey": output_key}
