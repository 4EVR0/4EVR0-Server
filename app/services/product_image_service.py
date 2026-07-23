import logging
from urllib.parse import quote

from app.core.config import settings

logger = logging.getLogger(__name__)


def build_product_image_url(goods_no: str | None) -> str | None:
    """Build a product image URL for an Olive Young goodsNo."""
    if not goods_no:
        return None

    normalized_goods_no = str(goods_no).strip()
    if not normalized_goods_no:
        return None

    key = _product_image_key(normalized_goods_no)
    if settings.product_image_url_mode.strip().lower() == "presigned":
        return _build_presigned_url(key)

    base_url = settings.product_image_base_url.strip().rstrip("/")
    if not base_url:
        return None

    relative_path = key.split("/", maxsplit=1)[-1]
    encoded_path = "/".join(quote(part, safe="=") for part in relative_path.split("/"))
    return f"{base_url}/{encoded_path}"


def _product_image_key(goods_no: str) -> str:
    prefix = settings.product_image_prefix.strip().strip("/")
    extension = settings.product_image_extension.strip()
    filename = f"main{extension}"
    path = f"goodsNo={goods_no}/{filename}"
    return f"{prefix}/{path}" if prefix else path


def _build_presigned_url(key: str) -> str | None:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        # presigned 모드인데 boto3 미설치 → 이미지 URL이 전부 None 이 된다.
        # 조용히 삼키면 "사진이 안 나오는" 원인 파악이 어려우니 명시적으로 경고한다.
        logger.warning(
            "presigned image URL mode is enabled but boto3 is not installed; "
            "product images will be unavailable. Add boto3 to requirements."
        )
        return None

    try:
        client_kwargs = {}
        if settings.aws_region:
            client_kwargs["region_name"] = settings.aws_region
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = settings.aws_access_key_id
            client_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

        client = boto3.client("s3", **client_kwargs)
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.product_image_bucket, "Key": key},
            ExpiresIn=settings.product_image_presigned_expires_seconds,
        )
    except (BotoCoreError, ClientError, NoCredentialsError) as exc:
        logger.warning("failed to generate presigned image URL: %s", exc)
        return None
