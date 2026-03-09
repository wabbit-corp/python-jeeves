import asyncio
import dataclasses
import datetime as dt
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from servant.defs import GlobalContext, RequestContext
from servant.modules import event_channels
from typed_json import JSONDict, require_obj


def _ctx_with_db(
    tmp_path: Path,
    *,
    user_id: str = "admin",
    channel_id: str = "1001",
    guild_id: str = "2001",
) -> GlobalContext:
    config: JSONDict = {
        "event_channels_db_path": str(tmp_path / "event_channels.sqlite3"),
        "admin_user_ids": [user_id],
    }
    ctx = GlobalContext(config=config)
    request = RequestContext(
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        is_dm=False,
    )
    return ctx.with_request(request)


@given(st.from_regex(r"UC[0-9A-Za-z_-]{6,40}", fullmatch=True))
def test_youtube_feed_url_from_uc_channel_id(channel_id: str) -> None:
    url = event_channels._youtube_feed_url_from_source(channel_id)
    assert url == f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def test_youtube_feed_url_from_channel_url() -> None:
    channel_id = "UC1234567890abcdef_-ABC"
    url = event_channels._youtube_feed_url_from_source(f"https://www.youtube.com/channel/{channel_id}")
    assert url == f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def test_parse_youtube_feed_extracts_entries() -> None:
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>vid2</yt:videoId>
    <title>Second</title>
    <published>2024-01-02T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/vid2"/>
  </entry>
  <entry>
    <yt:videoId>vid1</yt:videoId>
    <title>First</title>
    <published>2024-01-01T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/vid1"/>
  </entry>
</feed>
"""
    items = event_channels._parse_youtube_feed(feed)
    assert [i.item_id for i in items] == ["vid1", "vid2"]
    assert items[0].title == "First"
    assert items[0].url == "https://youtu.be/vid1"


def test_parse_ics_events_filters_by_window() -> None:
    now_utc = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:in-window
SUMMARY:In Window
DTSTART:20240102T000000Z
LOCATION:Room 1
END:VEVENT
BEGIN:VEVENT
UID:too-far
SUMMARY:Too Far
DTSTART:20240315T000000Z
END:VEVENT
BEGIN:VEVENT
UID:too-old
SUMMARY:Too Old
DTSTART:20231220T000000Z
END:VEVENT
END:VCALENDAR
"""
    items = event_channels._parse_ics_events(ics, now_utc=now_utc, lookahead_days=30)
    assert [i.item_id for i in items] == ["in-window"]
    assert items[0].title == "In Window (Room 1)"


def test_event_channel_subscription_manage_crud(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path)

    create_ics: JSONDict = {
        "operation": "create",
        "channel_id": "1001",
        "kind": "ics",
        "source": "https://example.com/calendar.ics",
    }
    create_result = asyncio.run(event_channels.event_channel_subscription_manage(ctx, create_ics))
    sub1 = require_obj(create_result["subscription"])
    assert isinstance(sub1["id"], int)
    assert sub1["kind"] == "ics"

    create_yt: JSONDict = {
        "operation": "create",
        "channel_id": "1001",
        "kind": "youtube",
        "source": "UC1234567890abcdef_-ABC",
        "check_every_seconds": 60,
        "max_posts_per_run": 1,
    }
    create_result = asyncio.run(event_channels.event_channel_subscription_manage(ctx, create_yt))
    sub2 = require_obj(create_result["subscription"])
    assert isinstance(sub2["id"], int)
    assert sub2["kind"] == "youtube"

    list_result = asyncio.run(
        event_channels.event_channel_subscription_manage(ctx, {"operation": "list", "channel_id": "1001"})
    )
    subs_value = list_result["subscriptions"]
    assert isinstance(subs_value, list)
    assert len(subs_value) == 2

    update_result = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {"operation": "update", "subscription_id": sub2["id"], "status": "disabled"},
        )
    )
    updated = require_obj(update_result["subscription"])
    assert updated["status"] == "disabled"

    cancel_result = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {"operation": "cancel", "subscription_id": sub1["id"]},
        )
    )
    cancelled = require_obj(cancel_result["subscription"])
    assert cancelled["status"] == "cancelled"


def test_poll_subscription_youtube_seeds_then_posts_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx_with_db(tmp_path)
    sent: list[tuple[str, str]] = []

    async def _send(channel_id: str, content: str) -> None:
        sent.append((channel_id, content))

    ctx.send_discord_message = _send

    create_payload: JSONDict = {
        "operation": "create",
        "channel_id": "1001",
        "kind": "youtube",
        "source": "UC1234567890abcdef_-ABC",
        "max_posts_per_run": 1,
    }
    create_result = asyncio.run(event_channels.event_channel_subscription_manage(ctx, create_payload))
    sub_payload = require_obj(create_result["subscription"])
    sid = sub_payload["id"]
    assert isinstance(sid, int)

    feed_seed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>v1</yt:videoId>
    <title>Old</title>
    <published>2024-01-01T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/v1"/>
  </entry>
  <entry>
    <yt:videoId>v2</yt:videoId>
    <title>Newer</title>
    <published>2024-01-02T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/v2"/>
  </entry>
</feed>
"""

    async def _fetch_seed(_ctx: GlobalContext, _url: str) -> str:
        return feed_seed

    monkeypatch.setattr(event_channels, "_fetch_text", _fetch_seed)

    dbfile = event_channels._db_path(ctx)
    conn = event_channels._connect(dbfile)
    try:
        row = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (sid,)).fetchone()
        assert row is not None
        sub = event_channels._row_to_subscription(row)

        newly_seen, posted = asyncio.run(event_channels._poll_subscription(ctx, conn=conn, sub=sub, now_ms=1000))
        assert newly_seen == 2
        assert posted == 0
        assert sent == []

        seen_rows = conn.execute(
            "SELECT item_id FROM event_channel_seen_items WHERE subscription_id = ? ORDER BY item_id ASC",
            (sid,),
        ).fetchall()
        assert [str(r["item_id"]) for r in seen_rows] == ["v1", "v2"]

        # Second poll: two unseen items arrive; max_posts_per_run=1 should post the newest one first.
        feed_update = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>v3</yt:videoId>
    <title>Newest</title>
    <published>2024-01-04T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/v3"/>
  </entry>
  <entry>
    <yt:videoId>v4</yt:videoId>
    <title>Newestest</title>
    <published>2024-01-05T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/v4"/>
  </entry>
  <entry>
    <yt:videoId>v2</yt:videoId>
    <title>Newer</title>
    <published>2024-01-02T00:00:00+00:00</published>
    <link rel="alternate" href="https://youtu.be/v2"/>
  </entry>
</feed>
"""

        async def _fetch_update(_ctx: GlobalContext, _url: str) -> str:
            return feed_update

        monkeypatch.setattr(event_channels, "_fetch_text", _fetch_update)
        sent.clear()

        sub2 = dataclasses.replace(sub, last_checked_at=1)
        newly_seen2, posted2 = asyncio.run(event_channels._poll_subscription(ctx, conn=conn, sub=sub2, now_ms=2000))
        assert posted2 == 1
        assert newly_seen2 == 1
        assert sent and sent[0][0] == "1001"
        assert "Newestest" in sent[0][1]

        seen_rows2 = conn.execute(
            "SELECT item_id FROM event_channel_seen_items WHERE subscription_id = ? ORDER BY item_id ASC",
            (sid,),
        ).fetchall()
        assert [str(r["item_id"]) for r in seen_rows2] == ["v1", "v2", "v4"]
    finally:
        conn.close()


def test_poll_subscription_ics_posts_soonest_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx_with_db(tmp_path)
    sent: list[str] = []

    async def _send(_channel_id: str, content: str) -> None:
        sent.append(content)

    ctx.send_discord_message = _send

    now = dt.datetime.now(tz=dt.timezone.utc)
    dt1 = now + dt.timedelta(hours=1)
    dt2 = now + dt.timedelta(hours=2)
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        "BEGIN:VEVENT\nUID:e1\nSUMMARY:Soon\nDTSTART:" + dt1.strftime("%Y%m%dT%H%M%SZ") + "\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:e2\nSUMMARY:Later\nDTSTART:" + dt2.strftime("%Y%m%dT%H%M%SZ") + "\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )

    async def _fetch(_ctx: GlobalContext, _url: str) -> str:
        return ics

    monkeypatch.setattr(event_channels, "_fetch_text", _fetch)

    create_payload: JSONDict = {
        "operation": "create",
        "channel_id": "1001",
        "kind": "ics",
        "source": "https://example.com/cal.ics",
        "max_posts_per_run": 1,
        "seed_on_first_run": False,
        "lookahead_days": 365,
    }
    create_result = asyncio.run(event_channels.event_channel_subscription_manage(ctx, create_payload))
    sub_payload = require_obj(create_result["subscription"])
    sid = sub_payload["id"]
    assert isinstance(sid, int)

    dbfile = event_channels._db_path(ctx)
    conn = event_channels._connect(dbfile)
    try:
        row = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (sid,)).fetchone()
        assert row is not None
        sub = event_channels._row_to_subscription(row)

        newly_seen, posted = asyncio.run(event_channels._poll_subscription(ctx, conn=conn, sub=sub, now_ms=1234))
        assert posted == 1
        assert newly_seen == 1
        assert sent and "Soon" in sent[0]
    finally:
        conn.close()
