"""Object storage for document bytes, behind the port.

Production: boto3 against any S3-compatible endpoint — AWS S3,
or SeaweedFS's S3 gateway in the compose stacks (service name
``minio``, endpoint ``http://minio:8333``). The bucket holds the
original submission bytes; the record store holds everything
decided about them. Tests and no-config runs use the in-memory
double, and ``/readiness`` names which one is live.
"""

from __future__ import annotations

import logging

from .config import env_str

log = logging.getLogger(__name__)


class InMemoryObjectStore:
    name = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._objects[key] = data
        return key

    def get(self, key: str) -> bytes:
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects


class S3ObjectStore:
    name = "s3"

    def __init__(self, bucket: str, endpoint_url: str | None, region: str) -> None:
        import boto3

        self._bucket = bucket
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region
        )

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )
        return key

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False


def default_object_store() -> InMemoryObjectStore | S3ObjectStore:
    """S3 when an endpoint/bucket is configured, else the double."""
    bucket = env_str("S3_BUCKET")
    endpoint = env_str("S3_ENDPOINT_URL")
    if bucket and endpoint:
        return S3ObjectStore(bucket, endpoint, env_str("S3_REGION", "us-east-1") or "us-east-1")
    return InMemoryObjectStore()
