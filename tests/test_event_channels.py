import asyncio
import datetime as dt
from pathlib import Path

import pytest

from servant.defs import GlobalContext, RequestContext
from servant.modules import event_channels
from typed_json import JSONDict, require_obj


def _ctx_with_db(
    tmp_path: Path,
    *,
    user_id: str = "123",
    channel_id: str = "111",
    guild_id: str | None = "222",
    admin_user_ids: list[str] | None = None,
) -> GlobalContext:
    config: JSONDict = {"event_channels_db_path": str(tmp_path / "event_channels.sqlite3")}
    if admin_user_ids is not None:
        config["admin_user_ids"] = [str(item) for item in admin_user_ids]
    ctx = GlobalContext(config=config)
    request = RequestContext(
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        is_dm=guild_id is None,
    )
    return ctx.with_request(request)


def test_unfold_ics_lines_merges_continuations() -> None:
    raw = "DESCRIPTION:This is folded \n over two lines\r\nSUMMARY:Done\r\n"
    lines = event_channels._unfold_ics_lines(raw)
    assert "DESCRIPTION:This is folded over two lines" in lines
    assert "SUMMARY:Done" in lines


def test_parse_ics_dt_parses_z_and_offset_and_date() -> None:
    assert event_channels._parse_ics_dt("20260206T180000Z", None) == dt.datetime(
        2026, 2, 6, 18, 0, tzinfo=dt.timezone.utc
    )
    assert event_channels._parse_ics_dt("20260206T130000-0500", None) == dt.datetime(
        2026, 2, 6, 18, 0, tzinfo=dt.timezone.utc
    )
    assert event_channels._parse_ics_dt("20260207", None) == dt.datetime(2026, 2, 7, 0, 0, tzinfo=dt.timezone.utc)


def test_parse_ics_events_filters_by_lookahead() -> None:
    ics = "\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "UID:abc123",
            "SUMMARY:Test Event",
            "LOCATION:Somewhere",
            "DTSTART:20260206T180000Z",
            "URL:https://example.com/event",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:def456",
            "SUMMARY:Too Far",
            "DTSTART:20260320T180000Z",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    now_utc = dt.datetime(2026, 2, 6, 0, 0, tzinfo=dt.timezone.utc)
    items = event_channels._parse_ics_events(ics, now_utc=now_utc, lookahead_days=7)
    assert [i.item_id for i in items] == ["abc123"]
    assert items[0].url == "https://example.com/event"
    assert "Somewhere" in items[0].title


def test_parse_youtube_feed_parses_entries() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>v1</yt:videoId>
    <title>First</title>
    <published>2026-02-01T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v1" />
  </entry>
  <entry>
    <yt:videoId>v2</yt:videoId>
    <title>Second</title>
    <published>2026-02-02T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v2" />
  </entry>
</feed>
"""
    items = event_channels._parse_youtube_feed(xml)
    assert [i.item_id for i in items] == ["v1", "v2"]
    assert items[0].url == "https://www.youtube.com/watch?v=v1"


def test_manage_create_list_update_cancel(tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path, admin_user_ids=["123"])

    created = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {
                "operation": "create",
                "channel_id": "111",
                "guild_id": "222",
                "kind": "youtube",
                "source": "UC1234567890123456789012",
                "check_every_seconds": 60,
                "max_posts_per_run": 2,
            },
        )
    )
    assert created["ok"] is True
    sub = require_obj(created["subscription"])
    assert sub["channel_id"] == "111"
    assert sub["kind"] == "youtube"

    listed = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {"operation": "list", "channel_id": "111"},
        )
    )
    assert listed["ok"] is True
    listed_subs = listed.get("subscriptions")
    assert isinstance(listed_subs, list)
    assert len(listed_subs) == 1

    sid_raw = sub.get("id")
    assert isinstance(sid_raw, int)
    sid = sid_raw
    updated = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {"operation": "update", "subscription_id": sid, "status": "disabled"},
        )
    )
    assert updated["ok"] is True
    updated_sub = require_obj(updated["subscription"])
    assert updated_sub["status"] == "disabled"

    cancelled = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {"operation": "cancel", "subscription_id": sid},
        )
    )
    assert cancelled["ok"] is True
    cancelled_sub = require_obj(cancelled["subscription"])
    assert cancelled_sub["status"] == "cancelled"


def test_poll_seeds_then_posts_new_youtube_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx_manage = _ctx_with_db(tmp_path, admin_user_ids=["123"])

    now_values = [1_000_000, 1_000_000, 1_006_000]

    def _fake_now_ms() -> int:
        return now_values.pop(0)

    monkeypatch.setattr(event_channels, "_now_ms", _fake_now_ms)

    feed_text: dict[str, str] = {}

    async def _fake_fetch_text(_ctx: GlobalContext, url: str) -> str:
        return feed_text[url]

    monkeypatch.setattr(event_channels, "_fetch_text", _fake_fetch_text)

    created = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx_manage,
            {
                "operation": "create",
                "channel_id": "111",
                "guild_id": "222",
                "kind": "youtube",
                "source": "UC1234567890123456789012",
                "check_every_seconds": 1,
                "max_posts_per_run": 5,
                "seed_on_first_run": True,
            },
        )
    )
    assert created["ok"] is True
    sub = require_obj(created["subscription"])
    source_url = str(sub["source"])

    feed_text[
        source_url
    ] = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>v1</yt:videoId>
    <title>First</title>
    <published>2026-02-01T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v1" />
  </entry>
  <entry>
    <yt:videoId>v2</yt:videoId>
    <title>Second</title>
    <published>2026-02-02T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v2" />
  </entry>
</feed>
"""

    posted_messages: list[str] = []

    async def _send(_channel_id: str, content: str) -> None:
        posted_messages.append(content)

    ctx_poll = GlobalContext(config=ctx_manage.config)
    ctx_poll.send_discord_message = _send

    # First poll seeds, no post.
    seeded = asyncio.run(event_channels.event_channels_poll_routine(ctx_poll, {}))
    assert seeded["ok"] is True
    assert seeded["posted"] == 0
    assert posted_messages == []

    # Second poll sees a new item.
    feed_text[
        source_url
    ] = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>v1</yt:videoId>
    <title>First</title>
    <published>2026-02-01T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v1" />
  </entry>
  <entry>
    <yt:videoId>v2</yt:videoId>
    <title>Second</title>
    <published>2026-02-02T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v2" />
  </entry>
  <entry>
    <yt:videoId>v3</yt:videoId>
    <title>Third</title>
    <published>2026-02-03T00:00:00+00:00</published>
    <link rel="alternate" href="https://www.youtube.com/watch?v=v3" />
  </entry>
</feed>
"""

    polled = asyncio.run(event_channels.event_channels_poll_routine(ctx_poll, {}))
    assert polled["ok"] is True
    assert polled["posted"] == 1
    assert len(posted_messages) == 1
    assert "v3" in posted_messages[0]


def test_poll_initial_ics_posts_last_week_and_next_week(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx_with_db(tmp_path, admin_user_ids=["123"])
    sent: list[str] = []

    async def _send(_channel_id: str, content: str) -> None:
        sent.append(content)

    ctx.send_discord_message = _send

    now = dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0)
    event_times = {
        "too-old": now - dt.timedelta(days=8),
        "past-week": now - dt.timedelta(days=6, hours=23),
        "recent-past": now - dt.timedelta(days=2),
        "soon": now + dt.timedelta(days=3),
        "next-week": now + dt.timedelta(days=6, hours=23),
        "too-far": now + dt.timedelta(days=8),
    }

    def _vevent(uid: str, summary: str, when: dt.datetime) -> str:
        return "\n".join(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"SUMMARY:{summary}",
                f"DTSTART:{when.strftime('%Y%m%dT%H%M%SZ')}",
                "END:VEVENT",
            ]
        )

    ics_text = "\n".join(
        [
            "BEGIN:VCALENDAR",
            _vevent("too-old", "Too Old", event_times["too-old"]),
            _vevent("past-week", "Past Week", event_times["past-week"]),
            _vevent("recent-past", "Recent Past", event_times["recent-past"]),
            _vevent("soon", "Soon", event_times["soon"]),
            _vevent("next-week", "Next Week", event_times["next-week"]),
            _vevent("too-far", "Too Far", event_times["too-far"]),
            "END:VCALENDAR",
            "",
        ]
    )

    async def _fetch_text(_ctx: GlobalContext, _url: str) -> str:
        return ics_text

    monkeypatch.setattr(event_channels, "_fetch_text", _fetch_text)

    create_result = asyncio.run(
        event_channels.event_channel_subscription_manage(
            ctx,
            {
                "operation": "create",
                "channel_id": "111",
                "guild_id": "222",
                "kind": "ics",
                "source": "https://example.com/calendar.ics",
                "seed_on_first_run": True,
                "lookahead_days": 30,
            },
        )
    )
    subscription = require_obj(create_result["subscription"])
    subscription_id = subscription.get("id")
    assert isinstance(subscription_id, int)

    dbfile = event_channels._db_path(ctx)
    conn = event_channels._connect(dbfile)
    try:
        row = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        assert row is not None
        sub = event_channels._row_to_subscription(row)

        now_ms = int(now.timestamp() * 1000)
        newly_seen, posted = asyncio.run(event_channels._poll_subscription(ctx, conn=conn, sub=sub, now_ms=now_ms))
        assert posted == 4
        assert newly_seen == 4
        assert len(sent) == 4
        assert any("Past Week" in msg for msg in sent)
        assert any("Recent Past" in msg for msg in sent)
        assert any("Soon" in msg for msg in sent)
        assert any("Next Week" in msg for msg in sent)
        assert all("Too Old" not in msg for msg in sent)
        assert all("Too Far" not in msg for msg in sent)
    finally:
        conn.close()
