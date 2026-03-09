from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from dateutil import tz
from dateutil.parser import isoparse
from defusedxml import ElementTree as ET

from servant import permissions
from servant.defs import (
    SECRET_USER_AGENT,
    GlobalContext,
    RoutineTask,
    ToolDef,
)
from typed_json import JSON, JSONDict, coerce_bool, coerce_int, coerce_snowflake, coerce_str

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = """
## Event Channels

Admins can configure "event channel" subscriptions that periodically poll:
- an ICS calendar URL (webcal/https)
- a YouTube channel Atom feed

When new items are detected, Vox will post them into the configured Discord channel.

Use `event_channel_subscription_manage` to create/update/cancel/list subscriptions.
"""

DEFAULT_DB_FILENAME = "servant_event_channels.sqlite3"

DEFAULT_CHECK_EVERY_SECONDS = 15 * 60
DEFAULT_MAX_POSTS_PER_RUN = 3
DEFAULT_LOOKAHEAD_DAYS = 30
DEFAULT_MAX_SUBSCRIPTIONS_PER_TICK = 10
DEFAULT_HTTP_TIMEOUT_SECONDS = 20
INITIAL_ICS_LOOKBACK_DAYS = 7
INITIAL_ICS_LOOKAHEAD_DAYS = 7
DEFAULT_ICS_LOOKBACK_DAYS = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_id(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_snowflake(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    parsed = coerce_snowflake(value, field)
    if parsed is None:
        raise ValueError(f"{field} must be a valid snowflake id.")
    return parsed


def _db_path(ctx: GlobalContext) -> Path:
    raw = ctx.config.get("event_channels_db_path")
    if raw:
        return Path(coerce_str(raw, field="event_channels_db_path", allow_empty=False)).expanduser().resolve()
    return (Path.cwd() / DEFAULT_DB_FILENAME).resolve()


def _connect(dbfile: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_channel_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            kind TEXT NOT NULL,                   -- ics|youtube
            source TEXT NOT NULL,                 -- url or feed url
            status TEXT NOT NULL DEFAULT 'active',-- active|disabled|cancelled

            check_every_seconds INTEGER NOT NULL DEFAULT 900,
            max_posts_per_run INTEGER NOT NULL DEFAULT 3,
            lookahead_days INTEGER NOT NULL DEFAULT 30,
            seed_on_first_run INTEGER NOT NULL DEFAULT 1,

            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_checked_at INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_channel_seen_items (
            subscription_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            item_ts INTEGER NOT NULL,             -- published/start timestamp in ms (best effort)
            seen_at INTEGER NOT NULL,
            PRIMARY KEY (subscription_id, item_id),
            FOREIGN KEY(subscription_id) REFERENCES event_channel_subscriptions(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_channel_subscriptions_active_due "
        "ON event_channel_subscriptions(status, last_checked_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_channel_subscriptions_channel "
        "ON event_channel_subscriptions(channel_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_channel_seen_items_ts "
        "ON event_channel_seen_items(subscription_id, item_ts);"
    )
    conn.commit()


def _get_user_agent(ctx: GlobalContext) -> str:
    raw = ctx.config.get(SECRET_USER_AGENT)
    if raw is None:
        return "python-jeeves (event_channels)"
    try:
        return coerce_str(raw, field=SECRET_USER_AGENT, allow_empty=False)
    except Exception:
        return "python-jeeves (event_channels)"


def _rewrite_webcal(url: str) -> str:
    text = url.strip()
    if text.lower().startswith("webcal://"):
        return "https://" + text[len("webcal://") :]
    return text


def _require_http_url(value: object, *, field: str) -> str:
    url = _rewrite_webcal(coerce_str(value, field=field, allow_empty=False))
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"{field} must be an http(s) URL.")
    return url


def _youtube_feed_url_from_source(source: str) -> str:
    text = source.strip()
    if not text:
        raise ValueError("YouTube source must be non-empty.")

    # Accept URLs and normalize common forms.
    if text.startswith("http://") or text.startswith("https://"):
        p = urlparse(text)
        host = (p.netloc or "").lower()
        if "youtube.com" in host:
            if p.path == "/feeds/videos.xml":
                channel_id = parse_qs(p.query).get("channel_id", [None])[0]
                if isinstance(channel_id, str) and channel_id.startswith("UC"):
                    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
                return text

            m = re.match(r"^/channel/(UC[0-9A-Za-z_-]+)", p.path)
            if m:
                return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
        return text

    # Accept channel ids directly (UC...)
    if text.startswith("UC"):
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={text}"
    raise ValueError("YouTube source must be a feed URL or a channel id starting with 'UC'.")


@dataclass(frozen=True)
class EventChannelSubscription:
    id: int
    guild_id: str
    channel_id: str
    kind: str
    source: str
    status: str
    check_every_seconds: int
    max_posts_per_run: int
    lookahead_days: int
    seed_on_first_run: bool
    created_at: int
    updated_at: int
    last_checked_at: int


def _row_to_subscription(r: sqlite3.Row) -> EventChannelSubscription:
    return EventChannelSubscription(
        id=int(r["id"]),
        guild_id=str(r["guild_id"]),
        channel_id=str(r["channel_id"]),
        kind=str(r["kind"]),
        source=str(r["source"]),
        status=str(r["status"]),
        check_every_seconds=int(r["check_every_seconds"]),
        max_posts_per_run=int(r["max_posts_per_run"]),
        lookahead_days=int(r["lookahead_days"]),
        seed_on_first_run=bool(int(r["seed_on_first_run"])),
        created_at=int(r["created_at"]),
        updated_at=int(r["updated_at"]),
        last_checked_at=int(r["last_checked_at"]),
    )


def _subscription_payload(s: EventChannelSubscription) -> JSONDict:
    return {
        "id": s.id,
        "guild_id": s.guild_id,
        "channel_id": s.channel_id,
        "kind": s.kind,
        "source": s.source,
        "status": s.status,
        "check_every_seconds": s.check_every_seconds,
        "max_posts_per_run": s.max_posts_per_run,
        "lookahead_days": s.lookahead_days,
        "seed_on_first_run": s.seed_on_first_run,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "last_checked_at": s.last_checked_at,
    }


def _require_int(obj: JSONDict, key: str) -> int:
    raw = obj.get(key)
    if raw is None:
        raise ValueError(f"{key} is required.")
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer.") from exc
    raise ValueError(f"{key} must be an integer.")


async def _infer_guild_id(ctx: GlobalContext, *, channel_id: str, guild_id: str | None) -> str:
    norm = _normalize_id(guild_id)
    if norm is not None:
        return norm

    request = ctx.request
    if request is not None and request.guild_id is not None and request.channel_id == channel_id:
        return request.guild_id

    resolved = await permissions.resolve_guild_id_for_channel(ctx, channel_id)
    if resolved is None:
        raise ValueError("guild_id could not be resolved for channel_id; provide guild_id explicitly.")
    return resolved


# ----------------------------
# Tool: create/update/cancel/list
# ----------------------------


async def event_channel_subscription_manage(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    op = obj.get("operation")
    if op not in {"create", "update", "cancel", "list"}:
        raise ValueError("operation must be one of: create, update, cancel, list")

    dbfile = _db_path(ctx)
    dbfile.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(dbfile)
    try:
        _init_db(conn)
        now = _now_ms()

        if op == "create":
            channel_id = _parse_snowflake(obj.get("channel_id"), "channel_id")
            if channel_id is None:
                request = permissions.require_request(ctx)
                channel_id = request.channel_id
            if channel_id is None:
                raise ValueError("channel_id is required.")

            await permissions.require_admin_for_channel(ctx, channel_id, "manage event channel subscriptions")

            kind = coerce_str(obj.get("kind"), field="kind", allow_empty=False).strip().lower()
            if kind not in {"ics", "youtube"}:
                raise ValueError("kind must be one of: ics, youtube")

            raw_source = obj.get("source")
            if raw_source is None:
                raise ValueError("source is required.")

            if kind == "ics":
                source = _require_http_url(raw_source, field="source")
            else:
                source = _youtube_feed_url_from_source(coerce_str(raw_source, field="source", allow_empty=False))

            guild_id = await _infer_guild_id(
                ctx, channel_id=channel_id, guild_id=_parse_snowflake(obj.get("guild_id"), "guild_id")
            )

            check_every_seconds = coerce_int(obj.get("check_every_seconds"), DEFAULT_CHECK_EVERY_SECONDS)
            if check_every_seconds <= 0:
                raise ValueError("check_every_seconds must be > 0.")

            max_posts_per_run = coerce_int(obj.get("max_posts_per_run"), DEFAULT_MAX_POSTS_PER_RUN)
            if max_posts_per_run <= 0:
                raise ValueError("max_posts_per_run must be > 0.")

            lookahead_days = coerce_int(obj.get("lookahead_days"), DEFAULT_LOOKAHEAD_DAYS)
            if lookahead_days <= 0:
                raise ValueError("lookahead_days must be > 0.")

            seed_on_first_run = coerce_bool(obj.get("seed_on_first_run"), True)

            cur = conn.execute(
                """
                INSERT INTO event_channel_subscriptions
                    (guild_id, channel_id, kind, source, status,
                     check_every_seconds, max_posts_per_run, lookahead_days, seed_on_first_run,
                     created_at, updated_at, last_checked_at)
                VALUES
                    (?, ?, ?, ?, 'active',
                     ?, ?, ?, ?,
                     ?, ?, 0)
                """,
                (
                    guild_id,
                    channel_id,
                    kind,
                    source,
                    check_every_seconds,
                    max_posts_per_run,
                    lookahead_days,
                    1 if seed_on_first_run else 0,
                    now,
                    now,
                ),
            )
            conn.commit()
            lastrowid = cur.lastrowid
            if lastrowid is None:
                raise RuntimeError("Failed to create subscription.")
            row = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (int(lastrowid),)).fetchone()
            if row is None:
                raise RuntimeError("Failed to load created subscription.")
            return {"ok": True, "subscription": _subscription_payload(_row_to_subscription(row))}

        if op == "update":
            sid = _require_int(obj, "subscription_id")
            row = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                raise ValueError(f"Subscription id={sid} not found.")
            existing = _row_to_subscription(row)

            await permissions.require_admin_for_channel(ctx, existing.channel_id, "manage event channel subscriptions")

            update_sets: list[str] = []
            update_params: list[object] = []

            if "source" in obj and obj["source"] is not None:
                raw_source = obj.get("source")
                if existing.kind == "ics":
                    source = _require_http_url(raw_source, field="source")
                else:
                    source = _youtube_feed_url_from_source(coerce_str(raw_source, field="source", allow_empty=False))
                update_sets.append("source = ?")
                update_params.append(source)

            if "status" in obj and obj["status"] is not None:
                status = coerce_str(obj.get("status"), field="status", allow_empty=False).strip().lower()
                if status not in {"active", "disabled", "cancelled"}:
                    raise ValueError("status must be one of: active, disabled, cancelled")
                update_sets.append("status = ?")
                update_params.append(status)

            if "check_every_seconds" in obj and obj["check_every_seconds"] is not None:
                check_every_seconds = coerce_int(obj.get("check_every_seconds"), existing.check_every_seconds)
                if check_every_seconds <= 0:
                    raise ValueError("check_every_seconds must be > 0.")
                update_sets.append("check_every_seconds = ?")
                update_params.append(check_every_seconds)

            if "max_posts_per_run" in obj and obj["max_posts_per_run"] is not None:
                max_posts = coerce_int(obj.get("max_posts_per_run"), existing.max_posts_per_run)
                if max_posts <= 0:
                    raise ValueError("max_posts_per_run must be > 0.")
                update_sets.append("max_posts_per_run = ?")
                update_params.append(max_posts)

            if "lookahead_days" in obj and obj["lookahead_days"] is not None:
                lookahead_days = coerce_int(obj.get("lookahead_days"), existing.lookahead_days)
                if lookahead_days <= 0:
                    raise ValueError("lookahead_days must be > 0.")
                update_sets.append("lookahead_days = ?")
                update_params.append(lookahead_days)

            if "seed_on_first_run" in obj and obj["seed_on_first_run"] is not None:
                seed_on_first_run = coerce_bool(obj.get("seed_on_first_run"), existing.seed_on_first_run)
                update_sets.append("seed_on_first_run = ?")
                update_params.append(1 if seed_on_first_run else 0)

            if not update_sets:
                raise ValueError("No updatable fields provided.")

            update_sets.append("updated_at = ?")
            update_params.append(now)
            update_params.append(sid)

            conn.execute(
                f"UPDATE event_channel_subscriptions SET {', '.join(update_sets)} WHERE id = ?",
                tuple(update_params),
            )
            conn.commit()
            row2 = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (sid,)).fetchone()
            if row2 is None:
                raise RuntimeError("Failed to load updated subscription.")
            return {"ok": True, "subscription": _subscription_payload(_row_to_subscription(row2))}

        if op == "cancel":
            sid = _require_int(obj, "subscription_id")
            row = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                raise ValueError(f"Subscription id={sid} not found.")
            existing = _row_to_subscription(row)

            await permissions.require_admin_for_channel(ctx, existing.channel_id, "manage event channel subscriptions")

            conn.execute(
                "UPDATE event_channel_subscriptions SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, sid),
            )
            conn.commit()
            row2 = conn.execute("SELECT * FROM event_channel_subscriptions WHERE id = ?", (sid,)).fetchone()
            if row2 is None:
                raise RuntimeError("Failed to load cancelled subscription.")
            return {"ok": True, "subscription": _subscription_payload(_row_to_subscription(row2))}

        # op == "list"
        channel_id_filter = _parse_snowflake(obj.get("channel_id"), "channel_id")
        guild_id_filter = _parse_snowflake(obj.get("guild_id"), "guild_id")
        status_filter = _normalize_id(obj.get("status"))
        kind_filter = _normalize_id(obj.get("kind"))

        if channel_id_filter is None and guild_id_filter is None:
            request = permissions.require_request(ctx)
            channel_id_filter = request.channel_id

        if channel_id_filter is not None:
            await permissions.require_admin_for_channel(ctx, channel_id_filter, "list event channel subscriptions")
        elif guild_id_filter is not None:
            await permissions.require_admin_for_guilds(ctx, guild_ids=[guild_id_filter], action="list event channels")
        else:
            request = permissions.require_request(ctx)
            if not permissions.is_global_admin(ctx, request.user_id):
                raise PermissionError(
                    "Admin privileges required to list subscriptions without a channel_id or guild_id."
                )

        where: list[str] = []
        query_params: list[object] = []
        if channel_id_filter is not None:
            where.append("channel_id = ?")
            query_params.append(channel_id_filter)
        if guild_id_filter is not None:
            where.append("guild_id = ?")
            query_params.append(guild_id_filter)
        if status_filter is not None:
            where.append("status = ?")
            query_params.append(status_filter)
        if kind_filter is not None:
            where.append("kind = ?")
            query_params.append(kind_filter)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM event_channel_subscriptions {clause} ORDER BY id DESC",
            tuple(query_params),
        ).fetchall()
        subscriptions: list[JSON] = [_subscription_payload(_row_to_subscription(r)) for r in rows]
        return {"ok": True, "subscriptions": subscriptions}
    finally:
        conn.close()


event_channel_subscription_manage_tool: ToolDef = ToolDef(
    name="event_channel_subscription_manage",
    function=lambda ctx, obj: event_channel_subscription_manage(ctx, obj),
    schema={
        "name": "event_channel_subscription_manage",
        "description": (
            "Admin-only: create/update/cancel/list event channel subscriptions that poll ICS calendars or YouTube feeds "
            "and post new items into Discord channels."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "update", "cancel", "list"]},
                "subscription_id": {"type": "integer", "description": "Required for update/cancel."},
                "channel_id": {"type": "string", "description": "Discord channel id to post into."},
                "guild_id": {"type": "string", "description": "Discord guild id (optional; inferred if possible)."},
                "kind": {"type": "string", "enum": ["ics", "youtube"]},
                "source": {
                    "type": "string",
                    "description": (
                        "For kind=ics: an http(s) or webcal URL to an .ics file. "
                        "For kind=youtube: either a YouTube Atom feed URL, or a channel id like 'UC...'."
                    ),
                },
                "status": {"type": "string", "enum": ["active", "disabled", "cancelled"]},
                "check_every_seconds": {"type": "integer", "description": "How often to poll (per subscription)."},
                "max_posts_per_run": {
                    "type": "integer",
                    "description": "Max posts per polling run for this subscription.",
                },
                "lookahead_days": {
                    "type": "integer",
                    "description": "For ICS: consider events starting within this window.",
                },
                "seed_on_first_run": {
                    "type": "boolean",
                    "description": (
                        "If true (default): first poll for ICS publishes the initialization window "
                        "(last 7 days + next 7 days), while non-ICS sources seed as seen without posting."
                    ),
                },
            },
            "required": ["operation"],
        },
    },
)


# ----------------------------
# Polling/parsing
# ----------------------------


@dataclass(frozen=True)
class _FeedItem:
    item_id: str
    title: str
    url: str | None
    item_ts_ms: int


def _unescape_ics_value(value: str) -> str:
    # RFC 5545 basic escaping for property values.
    # Order matters.
    text = value.replace("\\\\", "\\")
    text = text.replace("\\n", "\n").replace("\\N", "\n")
    text = text.replace("\\,", ",").replace("\\;", ";")
    return text


def _unfold_ics_lines(text: str) -> list[str]:
    # Unfold: lines beginning with space/tab continue previous line.
    # Preserve line order; strip trailing CR.
    out: list[str] = []
    previous: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith((" ", "\t")) and previous is not None:
            previous += line[1:]
            out[-1] = previous
            continue
        previous = line
        out.append(line)
    return out


def _parse_ics_dt(value: str, tzid: str | None) -> datetime | None:
    text = value.strip()
    if not text:
        return None

    # DATE (YYYYMMDD)
    if len(text) == 8 and text.isdigit():
        dt = datetime.strptime(text, "%Y%m%d")
        if tzid:
            tzinfo = tz.gettz(tzid)
            if tzinfo is not None:
                return dt.replace(tzinfo=tzinfo).astimezone(timezone.utc)
        return dt.replace(tzinfo=timezone.utc)

    # DATE-TIME (YYYYMMDDTHHMM[SS][Z|±HHMM])
    has_z = text.endswith("Z")
    base = text[:-1] if has_z else text
    offset_part: str | None = None
    if len(base) >= 5 and base[-5] in {"+", "-"} and base[-4:].isdigit():
        offset_part = base[-5:]
        base = base[:-5]

    fmt: str | None = None
    if len(base) == 13:
        fmt = "%Y%m%dT%H%M"
    elif len(base) == 15:
        fmt = "%Y%m%dT%H%M%S"
    if fmt is None:
        return None

    dt = datetime.strptime(base, fmt)
    if has_z:
        return dt.replace(tzinfo=timezone.utc)
    if offset_part is not None:
        sign = 1 if offset_part[0] == "+" else -1
        offset_hours = int(offset_part[1:3])
        offset_minutes = int(offset_part[3:5])
        offset = timedelta(hours=offset_hours, minutes=offset_minutes) * sign
        return dt.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)

    if tzid:
        tzinfo = tz.gettz(tzid)
        if tzinfo is not None:
            return dt.replace(tzinfo=tzinfo).astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _parse_ics_events(
    text: str,
    *,
    now_utc: datetime,
    lookahead_days: int,
    lookback_days: int = DEFAULT_ICS_LOOKBACK_DAYS,
) -> list[_FeedItem]:
    lines = _unfold_ics_lines(text)
    items: list[_FeedItem] = []

    in_event = False
    event_props: dict[str, tuple[str | None, str]] = {}

    def _flush_event() -> None:
        uid = event_props.get("UID", (None, ""))[1].strip()
        summary = _unescape_ics_value(event_props.get("SUMMARY", (None, ""))[1]).strip()
        if not summary:
            summary = "(no title)"

        tzid, dtstart_raw = event_props.get("DTSTART", (None, ""))
        dtstart = _parse_ics_dt(dtstart_raw, tzid)
        if dtstart is None:
            return
        if dtstart < now_utc - timedelta(days=lookback_days):
            return
        if dtstart > now_utc + timedelta(days=lookahead_days):
            return

        url = _unescape_ics_value(event_props.get("URL", (None, ""))[1]).strip() or None
        location = _unescape_ics_value(event_props.get("LOCATION", (None, ""))[1]).strip()
        if location:
            title = f"{summary} ({location})"
        else:
            title = summary

        item_id = uid or f"ics:{int(dtstart.timestamp())}:{summary}"
        items.append(_FeedItem(item_id=item_id, title=title, url=url, item_ts_ms=int(dtstart.timestamp() * 1000)))

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            event_props = {}
            continue
        if line == "END:VEVENT":
            if in_event:
                _flush_event()
            in_event = False
            event_props = {}
            continue
        if not in_event:
            continue
        if ":" not in line:
            continue
        left, value = line.split(":", 1)
        if not left:
            continue
        parts = left.split(";")
        key = parts[0].strip().upper()
        tzid: str | None = None
        for p in parts[1:]:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            if k.strip().upper() == "TZID":
                tzid = v.strip() or None
        if key in {"UID", "SUMMARY", "DTSTART", "DTEND", "LOCATION", "URL", "DESCRIPTION"}:
            event_props[key] = (tzid, value)
    items.sort(key=lambda i: i.item_ts_ms)
    return items


def _parse_youtube_feed(text: str) -> list[_FeedItem]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    ns_atom = "{http://www.w3.org/2005/Atom}"
    ns_yt = "{http://www.youtube.com/xml/schemas/2015}"

    items: list[_FeedItem] = []
    for entry in root.findall(f"{ns_atom}entry"):
        video_id_el = entry.find(f"{ns_yt}videoId")
        title_el = entry.find(f"{ns_atom}title")
        published_el = entry.find(f"{ns_atom}published")
        link_el = entry.find(f"{ns_atom}link[@rel='alternate']")

        video_id = (video_id_el.text if video_id_el is not None else None) or ""
        title = (title_el.text if title_el is not None else None) or ""
        published = (published_el.text if published_el is not None else None) or ""
        url = link_el.get("href") if link_el is not None else None

        video_id = video_id.strip()
        if not video_id:
            continue
        title = title.strip() or "(untitled)"
        try:
            published_dt = isoparse(published.strip()).astimezone(timezone.utc)
        except Exception:
            published_dt = datetime.now(tz=timezone.utc)
        items.append(
            _FeedItem(
                item_id=video_id,
                title=title,
                url=url,
                item_ts_ms=int(published_dt.timestamp() * 1000),
            )
        )
    items.sort(key=lambda i: i.item_ts_ms)
    return items


async def _fetch_text(ctx: GlobalContext, url: str) -> str:
    headers = {"User-Agent": _get_user_agent(ctx)}
    timeout = aiohttp.ClientTimeout(total=DEFAULT_HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            return await resp.text()


def _load_seen_ids(conn: sqlite3.Connection, subscription_id: int, item_ids: list[str]) -> set[str]:
    if not item_ids:
        return set()
    placeholders = ",".join(["?"] * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT item_id FROM event_channel_seen_items
        WHERE subscription_id = ? AND item_id IN ({placeholders})
        """,
        (subscription_id, *item_ids),
    ).fetchall()
    return {str(r["item_id"]) for r in rows}


def _mark_seen(conn: sqlite3.Connection, *, subscription_id: int, item: _FeedItem, seen_at: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO event_channel_seen_items
            (subscription_id, item_id, item_ts, seen_at)
        VALUES
            (?, ?, ?, ?)
        """,
        (subscription_id, item.item_id, item.item_ts_ms, seen_at),
    )


def _format_youtube_message(item: _FeedItem) -> str:
    if item.url:
        return f"New video: {item.title}\n{item.url}"
    return f"New video: {item.title}"


def _format_ics_message(item: _FeedItem) -> str:
    unix_s = int(item.item_ts_ms / 1000)
    when = f"<t:{unix_s}:F> (<t:{unix_s}:R>)"
    if item.url:
        return f"Event: {item.title} — {when}\n{item.url}"
    return f"Event: {item.title} — {when}"


def _preview_text(value: str, limit: int = 180) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


async def _poll_subscription(
    ctx: GlobalContext,
    *,
    conn: sqlite3.Connection,
    sub: EventChannelSubscription,
    now_ms: int,
) -> tuple[int, int]:
    """
    Returns (seen_count, posted_count).
    """
    if sub.status != "active":
        _LOGGER.debug("Skipping inactive event-channel subscription id=%s status=%s", sub.id, sub.status)
        return (0, 0)

    _LOGGER.info(
        "Polling event-channel subscription id=%s kind=%s channel=%s source=%s",
        sub.id,
        sub.kind,
        sub.channel_id,
        sub.source,
    )

    if sub.kind == "ics":
        is_first_run = sub.last_checked_at == 0 and sub.seed_on_first_run
        lookback_days = DEFAULT_ICS_LOOKBACK_DAYS
        lookahead_days = sub.lookahead_days
        if is_first_run:
            lookback_days = INITIAL_ICS_LOOKBACK_DAYS
            lookahead_days = INITIAL_ICS_LOOKAHEAD_DAYS
        try:
            body = await _fetch_text(ctx, sub.source)
        except Exception:
            _LOGGER.warning("Failed to fetch ICS source for subscription %s", sub.id, exc_info=True)
            return (0, 0)
        items = _parse_ics_events(
            body,
            now_utc=datetime.now(tz=timezone.utc),
            lookahead_days=lookahead_days,
            lookback_days=lookback_days,
        )
        _LOGGER.info(
            "Parsed ICS subscription id=%s entries=%s lookback_days=%s lookahead_days=%s",
            sub.id,
            len(items),
            lookback_days,
            lookahead_days,
        )
    elif sub.kind == "youtube":
        try:
            body = await _fetch_text(ctx, sub.source)
        except Exception:
            _LOGGER.warning("Failed to fetch YouTube source for subscription %s", sub.id, exc_info=True)
            return (0, 0)
        items = _parse_youtube_feed(body)
        if not items:
            feed_like = "<feed" in body and "youtube.com/xml/schemas/2015" in body
            if feed_like:
                _LOGGER.info("YouTube subscription id=%s feed has no entries.", sub.id)
            else:
                _LOGGER.warning(
                    "YouTube subscription id=%s source does not look like an Atom feed. source=%s preview=%r",
                    sub.id,
                    sub.source,
                    _preview_text(body),
                )
        else:
            latest_item = items[-1]
            _LOGGER.info(
                "Parsed YouTube subscription id=%s entries=%s latest_video=%s latest_time=%s",
                sub.id,
                len(items),
                latest_item.item_id,
                datetime.fromtimestamp(latest_item.item_ts_ms / 1000, tz=timezone.utc).isoformat(),
            )
    else:
        _LOGGER.error("Unknown subscription kind=%s for id=%s", sub.kind, sub.id)
        return (0, 0)

    if not items:
        _LOGGER.info("No candidate items to post for subscription id=%s", sub.id)
        return (0, 0)

    # First-run ICS behavior: publish a bounded initialization window.
    if sub.kind == "ics" and sub.last_checked_at == 0 and sub.seed_on_first_run:
        posted = 0
        newly_seen = 0
        for item in items:
            msg = _format_ics_message(item)
            try:
                if ctx.send_discord_message is None:
                    raise RuntimeError("ctx.send_discord_message not initialized.")
                await ctx.send_discord_message(sub.channel_id, msg)
            except Exception:
                _LOGGER.error(
                    "Failed to post initialization event for subscription %s to channel %s",
                    sub.id,
                    sub.channel_id,
                    exc_info=True,
                )
                continue
            _mark_seen(conn, subscription_id=sub.id, item=item, seen_at=now_ms)
            newly_seen += 1
            posted += 1
        if newly_seen:
            conn.commit()
        _LOGGER.info(
            "Initialization publish complete for ICS subscription id=%s posted=%s",
            sub.id,
            posted,
        )
        return (newly_seen, posted)

    # First-run seeding (non-ICS): mark current items as seen, but don't post.
    if sub.last_checked_at == 0 and sub.seed_on_first_run:
        for item in items:
            _mark_seen(conn, subscription_id=sub.id, item=item, seen_at=now_ms)
        conn.commit()
        _LOGGER.info(
            "Seeded first-run items for subscription id=%s kind=%s seeded=%s (no posts by design)",
            sub.id,
            sub.kind,
            len(items),
        )
        return (len(items), 0)

    item_ids = [i.item_id for i in items]
    already_seen = _load_seen_ids(conn, sub.id, item_ids)
    unseen_count = max(0, len(item_ids) - len(already_seen))
    _LOGGER.info(
        "Subscription id=%s seen=%s unseen=%s max_posts_per_run=%s",
        sub.id,
        len(already_seen),
        unseen_count,
        sub.max_posts_per_run,
    )

    if unseen_count == 0:
        _LOGGER.info("No new unseen items for subscription id=%s", sub.id)
        return (0, 0)

    items_for_posting = items
    if sub.kind == "youtube":
        items_for_posting = list(reversed(items))

    posted = 0
    newly_seen = 0
    for item in items_for_posting:
        if item.item_id in already_seen:
            continue
        if posted >= sub.max_posts_per_run:
            break

        msg = _format_ics_message(item) if sub.kind == "ics" else _format_youtube_message(item)
        try:
            if ctx.send_discord_message is None:
                raise RuntimeError("ctx.send_discord_message not initialized.")
            await ctx.send_discord_message(sub.channel_id, msg)
        except Exception:
            _LOGGER.error(
                "Failed to post new item for subscription %s to channel %s",
                sub.id,
                sub.channel_id,
                exc_info=True,
            )
            continue

        _mark_seen(conn, subscription_id=sub.id, item=item, seen_at=now_ms)
        newly_seen += 1
        posted += 1

    if newly_seen:
        conn.commit()
    _LOGGER.info(
        "Polling complete for subscription id=%s posted=%s newly_seen=%s",
        sub.id,
        posted,
        newly_seen,
    )
    return (newly_seen, posted)


def _get_int_config(ctx: GlobalContext, key: str, default: int) -> int:
    raw = ctx.config.get(key)
    if raw is None:
        return default
    value = coerce_int(raw, default)
    return value


async def event_channels_poll_routine(ctx: GlobalContext, _obj: JSON) -> JSONDict:
    dbfile = _db_path(ctx)
    if not dbfile.exists():
        _LOGGER.debug("Event channels DB does not exist yet: %s", dbfile)
        return {"ok": True, "checked": 0, "posted": 0}

    max_per_tick = _get_int_config(ctx, "event_channels_max_subscriptions_per_tick", DEFAULT_MAX_SUBSCRIPTIONS_PER_TICK)
    if max_per_tick <= 0:
        _LOGGER.debug("Event channels polling disabled via event_channels_max_subscriptions_per_tick=%s", max_per_tick)
        return {"ok": True, "checked": 0, "posted": 0}

    now_ms = _now_ms()
    conn = _connect(dbfile)
    try:
        _init_db(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM event_channel_subscriptions
            WHERE status = 'active'
              AND (last_checked_at = 0 OR (? - last_checked_at) >= (check_every_seconds * 1000))
            ORDER BY last_checked_at ASC, id ASC
            LIMIT ?
            """,
            (now_ms, max_per_tick),
        ).fetchall()

        subs = [_row_to_subscription(r) for r in rows]
        if not subs:
            _LOGGER.debug("No due event-channel subscriptions to poll.")
            return {"ok": True, "checked": 0, "posted": 0}

        _LOGGER.info("Polling %s due event-channel subscriptions.", len(subs))

        checked = 0
        posted = 0
        for sub in subs:
            checked += 1
            try:
                _seen, _posted = await _poll_subscription(ctx, conn=conn, sub=sub, now_ms=now_ms)
                posted += _posted
                _LOGGER.info(
                    "Polled subscription id=%s kind=%s seen=%s posted=%s",
                    sub.id,
                    sub.kind,
                    _seen,
                    _posted,
                )
            finally:
                conn.execute(
                    "UPDATE event_channel_subscriptions SET last_checked_at = ?, updated_at = ? WHERE id = ?",
                    (now_ms, now_ms, sub.id),
                )
                conn.commit()

        _LOGGER.info("Event-channel polling tick complete checked=%s posted=%s", checked, posted)
        return {"ok": True, "checked": checked, "posted": posted}
    finally:
        conn.close()


event_channels_poll_task: RoutineTask = RoutineTask(
    name="event_channels_poll",
    description="Poll configured event channel subscriptions and post new items.",
    run_every_seconds=30,
    function=event_channels_poll_routine,
)
