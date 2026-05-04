"""
Cloudflare R2 client for asset uploads.

Configured via env vars:
  R2_ACCOUNT_ID        — Cloudflare account ID
  R2_ACCESS_KEY_ID     — R2 API token key ID
  R2_SECRET_ACCESS_KEY — R2 API token secret
  R2_BUCKET_NAME       — bucket name (default: avbot-assets)
  R2_PUBLIC_URL        — public base URL e.g. https://cdn.avbot.app

Uploads are organised as: {guild_id}/{file_id}.{ext}
file_id is a uuid4 hex.
"""
import os
import uuid
import boto3
from botocore.client import Config

R2_ACCOUNT_ID        = os.getenv('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID     = os.getenv('R2_ACCESS_KEY_ID', '')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET_NAME       = os.getenv('R2_BUCKET_NAME', 'avbot-assets')
R2_PUBLIC_URL        = (os.getenv('R2_PUBLIC_URL', '') or '').rstrip('/')

ALLOWED_EXTENSIONS  = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

_client = None


def get_r2_client():
    global _client
    if _client is None:
        if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY]):
            raise RuntimeError('R2 credentials not configured')
        _client = boto3.client(
            's3',
            endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version='s3v4'),
            region_name='auto',
        )
    return _client


def upload_file(
    guild_id: int,
    filename: str,
    file_bytes: bytes,
    content_type: str = None,
) -> dict:
    """
    Upload a file to R2.
    Returns {file_id, key, url, size, content_type, extension}.
    Raises ValueError for bad input, RuntimeError if R2 is unconfigured.
    """
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f'File too large. Max {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB.'
        )

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f'File type not allowed: .{ext}. Allowed: {sorted(ALLOWED_EXTENSIONS)}'
        )

    file_id = uuid.uuid4().hex
    key = f'{guild_id}/{file_id}.{ext}'
    ct = content_type or _guess_content_type(ext)

    client = get_r2_client()
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=ct,
        CacheControl='public, max-age=31536000, immutable',
    )

    return {
        'file_id':      file_id,
        'key':          key,
        'url':          f'{R2_PUBLIC_URL}/{key}' if R2_PUBLIC_URL else None,
        'size':         len(file_bytes),
        'content_type': ct,
        'extension':    ext,
    }


def delete_file(key: str) -> bool:
    """Delete a file from R2. Returns True on success, False on error."""
    try:
        get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except Exception as e:
        print(f'[r2] delete failed for {key}: {e}')
        return False


def _guess_content_type(ext: str) -> str:
    return {
        'png':  'image/png',
        'jpg':  'image/jpeg',
        'jpeg': 'image/jpeg',
        'gif':  'image/gif',
        'webp': 'image/webp',
    }.get(ext, 'application/octet-stream')
