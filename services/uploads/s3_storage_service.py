import logging
import os
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


def _bucket_name() -> str:
    bucket = os.getenv("AWS_S3_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME")
    if not bucket:
        raise ValueError("AWS_S3_BUCKET_NAME environment variable is not configured.")
    return bucket


def _aws_region() -> str:
    return os.getenv("AWS_S3_REGION") or os.getenv("AWS_REGION", "ca-central-1")


def _is_enabled(value: Optional[str], *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _request_security_args() -> dict[str, Any]:
    expected_owner = os.getenv("AWS_S3_EXPECTED_BUCKET_OWNER", "").strip()
    return {"ExpectedBucketOwner": expected_owner} if expected_owner else {}


def _upload_security_args() -> dict[str, Any]:
    algorithm = os.getenv("AWS_S3_SERVER_SIDE_ENCRYPTION", "AES256").strip()
    if algorithm not in {"AES256", "aws:kms"}:
        raise ValueError(
            "AWS_S3_SERVER_SIDE_ENCRYPTION must be 'AES256' or 'aws:kms'."
        )

    args: dict[str, Any] = {
        "ServerSideEncryption": algorithm,
        "ChecksumAlgorithm": "SHA256",
        "CacheControl": "private, no-store",
        **_request_security_args(),
    }
    if algorithm == "aws:kms":
        kms_key_id = os.getenv("AWS_S3_KMS_KEY_ID", "").strip()
        if kms_key_id:
            args["SSEKMSKeyId"] = kms_key_id
        args["BucketKeyEnabled"] = _is_enabled(
            os.getenv("AWS_S3_BUCKET_KEY_ENABLED"),
            default=True,
        )
    return args

def get_s3_client():
    """Initializes and returns a boto3 S3 client using environment or IAM role credentials."""
    access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    
    kwargs = {"region_name": _aws_region(), "use_ssl": True}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    return boto3.client("s3", **kwargs)

def upload_file_to_s3(file_bytes: bytes, s3_key: str, content_type: Optional[str] = None) -> str:
    """
    Uploads file bytes to S3.
    Returns the S3 URL of the uploaded file.
    """
    bucket = _bucket_name()
    s3_client = get_s3_client()
    extra_args = _upload_security_args()
    if content_type:
        extra_args["ContentType"] = content_type

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=file_bytes,
            **extra_args,
        )
        url = f"https://{bucket}.s3.{_aws_region()}.amazonaws.com/{s3_key}"
        logger.info("Successfully uploaded file to S3: %s", s3_key)
        return url
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to upload %s to S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 upload error: {exc}") from exc

def download_file_from_s3(s3_key: str) -> bytes:
    """
    Downloads file content from S3 as bytes.
    """
    bucket = _bucket_name()
    s3_client = get_s3_client()
    try:
        response = s3_client.get_object(
            Bucket=bucket,
            Key=s3_key,
            **_request_security_args(),
        )
        return response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to download %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 download error: {exc}") from exc

def generate_presigned_url(s3_key: str, expiration: int = 3600) -> str:
    """
    Generates a secure temporary pre-signed URL to view/download a file from S3.
    """
    bucket = _bucket_name()
    s3_client = get_s3_client()
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
                **_request_security_args(),
            },
            ExpiresIn=expiration,
        )
        return url
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to generate presigned URL for %s: %s", s3_key, exc)
        raise RuntimeError(f"S3 presigned URL error: {exc}") from exc

def delete_file_from_s3(s3_key: str) -> bool:
    """
    Deletes a file object from S3.
    """
    bucket = _bucket_name()
    s3_client = get_s3_client()
    try:
        s3_client.delete_object(
            Bucket=bucket,
            Key=s3_key,
            **_request_security_args(),
        )
        logger.info("Successfully deleted %s from S3", s3_key)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to delete %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 delete error: {exc}") from exc
