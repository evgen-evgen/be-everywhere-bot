import logging
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, PublishResult
from config import FACEBOOK_APP, NETWORK_FACEBOOK
from db.accounts import (
    Account,
    create_account,
    find_account,
    get_all_credentials,
    set_credentials,
    update_remote_id,
)
from utils.http_utils import format_api_error, parse_error_detail

logger = logging.getLogger(__name__)

AUTH_HELP = """\
facebook:
  Publishes to a Facebook Page (personal profiles are not supported).
  Prompts for a Page access token with pages_manage_posts and the Page ID.
"""


def _require_creds(engine: Engine, account_id: int) -> dict[str, str]:
    creds = get_all_credentials(engine, account_id)
    missing = [key for key in ("page_access_token", "page_id") if not creds.get(key)]
    if missing:
        raise RuntimeError(
            f"Facebook account {account_id} not configured "
            f"(missing: {', '.join(missing)}). "
            "Run: uv run python main.py --auth=facebook"
        )
    return creds


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    access_token: str,
    *,
    data: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> dict[str, Any]:
    response = await client.request(
        method,
        f"{FACEBOOK_APP.api_base_url}/{path.lstrip('/')}",
        data={**(data or {}), "access_token": access_token},
        files=files,
    )
    if not response.is_success:
        raise RuntimeError(
            format_api_error("Facebook", response.status_code, parse_error_detail(response))
        )
    return response.json()


async def _lookup_page(page_id: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{FACEBOOK_APP.api_base_url}/{page_id}",
            params={"fields": "id,name", "access_token": token},
        )
    if not response.is_success:
        raise RuntimeError(
            format_api_error("Facebook", response.status_code, parse_error_detail(response))
        )
    page = response.json()
    if not page.get("id"):
        raise RuntimeError("Facebook Page lookup failed: missing id")
    return page


async def authenticate(engine: Engine, label: str = "default") -> Account:
    print(AUTH_HELP)
    token = input("Page access token: ").strip()
    page_id = input("Facebook Page ID: ").strip()
    if not token or not page_id:
        raise ValueError("Page access token and Page ID are required")

    page = await _lookup_page(page_id, token)
    page_id = str(page["id"])
    page_name = str(page.get("name") or page_id)
    creds = {
        "page_access_token": token,
        "page_id": page_id,
        "page_name": page_name,
    }
    existing = find_account(engine, NETWORK_FACEBOOK, label)
    if existing:
        set_credentials(engine, existing.id, creds)
        update_remote_id(engine, existing.id, page_id)
        print(f"Facebook account '{label}' updated for Page {page_name}")
        return existing

    account = create_account(engine, NETWORK_FACEBOOK, label, page_id)
    set_credentials(engine, account.id, creds)
    print(f"Facebook account '{label}' configured for Page {page_name}")
    return account


def _content_type(item: MediaItem) -> str:
    return "video/mp4" if item.media_type in ("video", "animated_gif") else "image/jpeg"


async def _publish_photos(
    client: httpx.AsyncClient,
    page_id: str,
    token: str,
    text: str,
    media: list[MediaItem],
    raw_items: list[bytes],
) -> str:
    photo_ids: list[str] = []
    for index, (item, raw) in enumerate(zip(media, raw_items)):
        result = await _request(
            client,
            "POST",
            f"{page_id}/photos",
            token,
            data={"published": "false"},
            files={"source": (f"photo-{index}.jpg", raw, _content_type(item))},
        )
        photo_ids.append(str(result["id"]))

    data: dict[str, Any] = {"message": text}
    for index, photo_id in enumerate(photo_ids):
        data[f"attached_media[{index}]"] = f'{{"media_fbid":"{photo_id}"}}'
    result = await _request(client, "POST", f"{page_id}/feed", token, data=data)
    return str(result["id"])


async def publish_outbound(
    engine: Engine,
    account_id: int,
    outbound: OutboundPost,
    media_bytes: list[bytes] | None = None,
    *,
    reply_to: str | None = None,
) -> PublishResult:
    creds = _require_creds(engine, account_id)
    token, page_id = creds["page_access_token"], creds["page_id"]
    raw_items = media_bytes or []
    if len(raw_items) != len(outbound.media):
        raise RuntimeError(
            f"Facebook media upload mismatch: {len(outbound.media)} attachment(s), "
            f"{len(raw_items)} downloaded"
        )

    async with httpx.AsyncClient(timeout=120.0) as client:
        if reply_to:
            if outbound.media:
                logger.warning("Facebook continuation media skipped; comments are text-only")
            result = await _request(
                client, "POST", f"{reply_to}/comments", token,
                data={"message": outbound.text},
            )
            comment_id = str(result["id"])
            return PublishResult(post_id=comment_id, reply_ref=reply_to)

        photos = [item.media_type == "photo" for item in outbound.media]
        if outbound.media and all(photos):
            post_id = await _publish_photos(
                client, page_id, token, outbound.text, outbound.media, raw_items
            )
        elif outbound.media:
            post_id = ""
            for index, (item, raw) in enumerate(zip(outbound.media, raw_items)):
                result = await _request(
                    client,
                    "POST",
                    f"{page_id}/videos",
                    token,
                    data={"description": outbound.text if index == 0 else ""},
                    files={
                        "source": (
                            f"video-{index}.mp4",
                            raw,
                            _content_type(item),
                        )
                    },
                )
                post_id = str(result["id"])
        else:
            result = await _request(
                client, "POST", f"{page_id}/feed", token,
                data={"message": outbound.text},
            )
            post_id = str(result["id"])
        return PublishResult(post_id=post_id, reply_ref=post_id)


async def download_media(media: MediaItem, engine: Engine, account_id: int) -> bytes:
    raise RuntimeError("Facebook is a publish-only destination")
