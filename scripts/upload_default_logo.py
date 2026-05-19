"""One-time helper: upload the AV default logo to R2 at a stable path.

Usage (from project root, with R2 env vars set):
    python scripts/upload_default_logo.py

Place the PNG at assets/av_logo.png (or pass --file path/to/logo.png).
Resulting public URL is logged on success; ensure config.DEFAULT_BOT_THUMBNAIL_URL
matches that URL (it currently expects https://cdn.avbot.app/defaults/av_logo.png).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from r2_client import get_r2_client, R2_BUCKET_NAME, R2_PUBLIC_URL


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', default=str(ROOT / 'assets' / 'av_logo.png'),
                        help='Path to the AV logo PNG (default: assets/av_logo.png).')
    parser.add_argument('--key', default='defaults/av_logo.png',
                        help='Object key in the R2 bucket.')
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f'ERROR: {src} does not exist. Place the logo there first.')
        return 1

    data = src.read_bytes()
    client = get_r2_client()
    client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=args.key,
        Body=data,
        ContentType='image/png',
        CacheControl='public, max-age=31536000, immutable',
    )

    public_url = f'{R2_PUBLIC_URL}/{args.key}' if R2_PUBLIC_URL else f'(no public base configured) /{args.key}'
    print(f'OK — uploaded {len(data)} bytes to bucket={R2_BUCKET_NAME!r} key={args.key!r}')
    print(f'Public URL: {public_url}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
