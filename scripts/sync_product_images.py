"""Copy registered product raster images using operator AWS authentication.

No credentials belong in this cache or in the app. Caddy mounts the output read-only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def sync(client, goods, output, bucket, prefix):
    def copy(goods_no):
        directory = output / ('goodsNo=' + goods_no)
        candidates = [directory / ('main.' + ext) for ext in ('jpg', 'png', 'webp', 'gif')]
        if any(p.is_file() for p in candidates):
            return goods_no, None
        data = None
        for ext in ('jpg', 'png', 'webp', 'gif'):
            try:
                obj = client.get_object(Bucket=bucket, Key=f'{prefix}/goodsNo={goods_no}/main.{ext}')
                with obj['Body'] as stream:
                    data = stream.read(5_000_001)
                break
            except ClientError as error:
                if error.response['Error']['Code'] != 'NoSuchKey':
                    return goods_no, error.response['Error']['Code']
        if data is None:
            return goods_no, 'NoSuchKey'
        if len(data) > 5_000_000:
            return goods_no, 'ImageTooLarge'
        if data.startswith(b'\xff\xd8'):
            ext = 'jpg'
        elif data.startswith(b'\x89PNG\r\n\x1a\n'):
            ext = 'png'
        elif data.startswith((b'GIF87a', b'GIF89a')):
            ext = 'gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            ext = 'webp'
        else:
            return goods_no, 'UnsupportedImage'
        directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        path = directory / ('main.' + ext)
        temporary = directory / ('main.' + ext + '.tmp')
        temporary.write_bytes(data)
        temporary.chmod(0o644)
        temporary.replace(path)
        return goods_no, None

    errors = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for goods_no, error in pool.map(copy, goods):
            if error:
                errors[goods_no] = error
    return {'products': len(goods), 'cached': len(goods) - len(errors), 'missing': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goods-file', type=Path, required=True, help='JSON array of registered goods numbers')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--profile', help='Existing operator AWS profile; never an access key')
    parser.add_argument('--bucket', default='oliveyoung-crawl-data')
    parser.add_argument('--prefix', default='oliveyoung_images')
    args = parser.parse_args()
    goods = sorted(set(json.loads(args.goods_file.read_text())))
    if not all(isinstance(g, str) and re.fullmatch('[AB][0-9]{12}', g) for g in goods):
        parser.error('Only registered Olive Young goods numbers are accepted')
    client = boto3.Session(profile_name=args.profile).client('s3', config=Config(max_pool_connections=12))
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    result = sync(client, goods, args.output_dir, args.bucket, args.prefix)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
