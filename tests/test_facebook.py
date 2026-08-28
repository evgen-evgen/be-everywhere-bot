import httpx
import pytest
import respx

from apis.facebook import publish_outbound
from apis.types import MediaItem, OutboundPost
from config import NETWORK_FACEBOOK
from db.accounts import create_account, set_credentials


def _account(engine):
    account = create_account(engine, NETWORK_FACEBOOK, "main", "page-1")
    set_credentials(engine, account.id, {"page_access_token": "page-token", "page_id": "page-1", "page_name": "Test Page"})
    return account


@pytest.mark.asyncio
@respx.mock
async def test_publish_text(engine):
    account = _account(engine)
    route = respx.post("https://graph.facebook.com/v24.0/page-1/feed").mock(
        return_value=httpx.Response(200, json={"id": "page-1_100"})
    )
    result = await publish_outbound(engine, account.id, OutboundPost(text="Hello Facebook"), [])
    assert result.post_id == "page-1_100"
    assert b"Hello+Facebook" in route.calls[0].request.content
    assert b"page-token" in route.calls[0].request.content


@pytest.mark.asyncio
@respx.mock
async def test_publish_photo_album(engine):
    account = _account(engine)
    photos = respx.post("https://graph.facebook.com/v24.0/page-1/photos").mock(
        side_effect=[httpx.Response(200, json={"id": "photo-1"}), httpx.Response(200, json={"id": "photo-2"})]
    )
    feed = respx.post("https://graph.facebook.com/v24.0/page-1/feed").mock(
        return_value=httpx.Response(200, json={"id": "page-1_101"})
    )
    media = [MediaItem(url="a", media_type="photo"), MediaItem(url="b", media_type="photo")]
    result = await publish_outbound(engine, account.id, OutboundPost(text="Album", media=media), [b"one", b"two"])
    assert result.post_id == "page-1_101"
    assert photos.call_count == 2
    assert b"photo-1" in feed.calls[0].request.content


@pytest.mark.asyncio
@respx.mock
async def test_publish_continuation_as_comment(engine):
    account = _account(engine)
    route = respx.post("https://graph.facebook.com/v24.0/page-1_100/comments").mock(
        return_value=httpx.Response(200, json={"id": "comment-1"})
    )
    result = await publish_outbound(engine, account.id, OutboundPost(text="continued"), [], reply_to="page-1_100")
    assert result.post_id == "comment-1"
    assert result.reply_ref == "page-1_100"
    assert route.called
