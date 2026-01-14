from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from servant.defs import RoutineTask, ToolDef, GlobalContext
from typed_json import JSON, JSONDict, coerce_int, coerce_str

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = (
    "Commitments module: create/update/cancel user commitments and periodically "
    "check-in with users in the channel they committed in."
)

DEFAULT_INTERVAL_DAYS = 5
DEFAULT_DB_FILENAME = "servant_commitments.sqlite3"
_WARNED_MISSING_COMMITMENT_COLUMNS: set[str] = set()


# ----------------------------
# Storage
# ----------------------------


def _db_path(ctx: GlobalContext) -> Path:
    """
    Where to put the sqlite file.
    Override by setting ctx.secrets['commitments_db_path'] to an absolute path.
    """
    raw = ctx.secrets.get("commitments_db_path")
    if raw:
        return Path(coerce_str(raw, field="commitments_db_path", allow_empty=False)).expanduser().resolve()
    return (Path.cwd() / DEFAULT_DB_FILENAME).resolve()


def _connect(dbfile: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _parse_epoch_ms(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            try:
                return int(text)
            except ValueError:
                return None
        try:
            if text.endswith("Z"):
                text = text[:-1]
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        else:
            parsed = parsed.astimezone(dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


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


def _migrate_timestamp_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT rowid AS _rowid, created_at, updated_at
        FROM commitments
        WHERE (created_at IS NOT NULL AND typeof(created_at) IN ('text', 'real'))
           OR (updated_at IS NOT NULL AND typeof(updated_at) IN ('text', 'real'))
        """
    ).fetchall()

    for row in rows:
        rowid = row["_rowid"] if "_rowid" in row.keys() else None
        if rowid is None:
            _LOGGER.warning("Commitments migration row missing rowid; skipping timestamp migration.")
            continue
        updates: dict[str, int] = {}
        created_ms = _parse_epoch_ms(row["created_at"])
        updated_ms = _parse_epoch_ms(row["updated_at"])
        if created_ms is not None:
            updates["created_at"] = created_ms
        if updated_ms is not None:
            updates["updated_at"] = updated_ms
        if not updates:
            continue
        columns = ", ".join(f"{col} = ?" for col in updates.keys())
        params = list(updates.values()) + [rowid]
        conn.execute(
            f"UPDATE commitments SET {columns} WHERE rowid = ?",
            params,
        )


def _ensure_commitments_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(commitments)")}
    now: int | None = None

    def _now() -> int:
        nonlocal now
        if now is None:
            now = _now_ms()
        return now

    if "interval_days" not in columns:
        _LOGGER.info("Adding missing column commitments.interval_days")
        conn.execute("ALTER TABLE commitments ADD COLUMN interval_days INTEGER NOT NULL DEFAULT 5;")
        columns.add("interval_days")
    if "interval_days" in columns:
        conn.execute(
            "UPDATE commitments SET interval_days = ? WHERE interval_days IS NULL",
            (DEFAULT_INTERVAL_DAYS,),
        )

    if "last_checkin_date" not in columns:
        _LOGGER.info("Adding missing column commitments.last_checkin_date")
        conn.execute("ALTER TABLE commitments ADD COLUMN last_checkin_date TEXT;")
        columns.add("last_checkin_date")

    if "status" not in columns:
        _LOGGER.info("Adding missing column commitments.status")
        conn.execute("ALTER TABLE commitments ADD COLUMN status TEXT NOT NULL DEFAULT 'active';")
        columns.add("status")
    if "status" in columns:
        conn.execute("UPDATE commitments SET status = 'active' WHERE status IS NULL")

    if "created_at" not in columns:
        _LOGGER.info("Adding missing column commitments.created_at")
        conn.execute("ALTER TABLE commitments ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0;")
        columns.add("created_at")
    if "created_at" in columns:
        conn.execute(
            "UPDATE commitments SET created_at = ? WHERE created_at IS NULL OR created_at = 0",
            (_now(),),
        )

    if "updated_at" not in columns:
        _LOGGER.info("Adding missing column commitments.updated_at")
        conn.execute("ALTER TABLE commitments ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;")
        columns.add("updated_at")
    if "updated_at" in columns:
        conn.execute(
            "UPDATE commitments SET updated_at = ? WHERE updated_at IS NULL OR updated_at = 0",
            (_now(),),
        )


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            start_date TEXT NOT NULL,               -- YYYY-MM-DD
            end_date TEXT NOT NULL,                 -- YYYY-MM-DD
            interval_days INTEGER NOT NULL DEFAULT 5,
            last_checkin_date TEXT NULL,            -- YYYY-MM-DD
            status TEXT NOT NULL DEFAULT 'active',  -- active|cancelled|ended
            created_at INTEGER NOT NULL,            -- epoch ms
            updated_at INTEGER NOT NULL             -- epoch ms
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_active_channel " "ON commitments(status, channel_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_user " "ON commitments(user_id);")
    _ensure_commitments_columns(conn)
    _migrate_timestamp_columns(conn)
    conn.commit()


def _today_yyyy_mm_dd() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


@dataclass(frozen=True)
class Commitment:
    id: int
    name: str
    description: str
    user_id: str
    channel_id: str
    start_date: str
    end_date: str
    interval_days: int
    last_checkin_date: str | None
    status: str


def _commitment_payload(commitment: Commitment) -> JSONDict:
    return {
        "id": commitment.id,
        "name": commitment.name,
        "description": commitment.description,
        "user_id": commitment.user_id,
        "channel_id": commitment.channel_id,
        "start_date": commitment.start_date,
        "end_date": commitment.end_date,
        "interval_days": commitment.interval_days,
        "last_checkin_date": commitment.last_checkin_date,
        "status": commitment.status,
    }


def _row_to_commitment(r: sqlite3.Row) -> Commitment:
    keys = set(r.keys())
    missing_optional: list[str] = []

    raw_interval = r["interval_days"] if "interval_days" in keys else None
    if raw_interval is None:
        interval_days = DEFAULT_INTERVAL_DAYS
        if "interval_days" not in keys:
            missing_optional.append("interval_days")
    else:
        try:
            interval_days = int(raw_interval)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Invalid interval_days %r for commitment id=%s; using default %s",
                raw_interval,
                r["id"] if "id" in keys else "unknown",
                DEFAULT_INTERVAL_DAYS,
            )
            interval_days = DEFAULT_INTERVAL_DAYS

    if "last_checkin_date" in keys:
        last_checkin_date = r["last_checkin_date"]
    else:
        last_checkin_date = None
        missing_optional.append("last_checkin_date")

    if "status" in keys and r["status"] is not None:
        status = str(r["status"])
    else:
        status = "active"
        if "status" not in keys:
            missing_optional.append("status")

    if missing_optional:
        new_missing = sorted(set(missing_optional) - _WARNED_MISSING_COMMITMENT_COLUMNS)
        if new_missing:
            _LOGGER.warning(
                "Commitments row missing columns %s (row id=%s). Using defaults.",
                ", ".join(new_missing),
                r["id"] if "id" in keys else "unknown",
            )
            _WARNED_MISSING_COMMITMENT_COLUMNS.update(new_missing)

    return Commitment(
        id=int(r["id"]),
        name=str(r["name"]),
        description=str(r["description"]),
        user_id=str(r["user_id"]),
        channel_id=str(r["channel_id"]),
        start_date=str(r["start_date"]),
        end_date=str(r["end_date"]),
        interval_days=interval_days,
        last_checkin_date=last_checkin_date,
        status=status,
    )


async def _with_db(ctx: GlobalContext, fn: Callable[[sqlite3.Connection], JSONDict]) -> JSONDict:
    dbfile = _db_path(ctx)

    def _run() -> JSONDict:
        dbfile.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(dbfile)
        try:
            _init_db(conn)
            return fn(conn)
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


def _mention(user_id: str) -> str:
    # Discord mention format
    return f"<@{user_id}>"


# ----------------------------
# Tool: create/update/cancel/list
# ----------------------------


async def commitment_manage(ctx: GlobalContext, obj: JSON) -> JSONDict:
    """
    Single tool that can:
      - create a commitment
      - update fields (including channel, end date, name, description)
      - cancel a commitment
      - list commitments (optional convenience)
    """
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    op = obj.get("operation")
    if op not in {"create", "update", "cancel", "list"}:
        raise ValueError("operation must be one of: create, update, cancel, list")

    def _logic(conn: sqlite3.Connection) -> JSONDict:
        now = _now_ms()

        if op == "create":
            name = str(obj["name"])
            description = str(obj["description"])
            user_id = str(obj["user_id"])
            channel_id = str(obj["channel_id"])
            start_date = str(obj.get("start_date") or _today_yyyy_mm_dd())
            end_date = str(obj["end_date"]) if "end_date" in obj else None
            interval_days = coerce_int(obj.get("interval_days"), DEFAULT_INTERVAL_DAYS)
            if interval_days <= 0:
                interval_days = DEFAULT_INTERVAL_DAYS

            sd = _parse_date(start_date)
            ed = _parse_date(end_date) if end_date else sd + dt.timedelta(days=30)
            if ed < sd:
                raise ValueError("end_date must be >= start_date")

            cur = conn.execute(
                """
                INSERT INTO commitments
                    (name, description, user_id, channel_id, start_date, end_date,
                     interval_days, last_checkin_date, status, created_at, updated_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)
                """,
                (
                    name,
                    description,
                    user_id,
                    channel_id,
                    start_date,
                    end_date,
                    interval_days,
                    now,
                    now,
                ),
            )
            conn.commit()
            lastrowid = cur.lastrowid
            if lastrowid is None:
                raise RuntimeError("Failed to create commitment.")
            cid = int(lastrowid)
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
            return {
                "ok": True,
                "commitment": _commitment_payload(_row_to_commitment(row)),
            }

        if op == "update":
            cid = _require_int(obj, "commitment_id")

            # Build dynamic update set from provided fields.
            allowed = {
                "name": "name",
                "description": "description",
                "user_id": "user_id",
                "channel_id": "channel_id",
                "start_date": "start_date",
                "end_date": "end_date",
                "interval_days": "interval_days",
                "status": "status",
            }

            sets: list[str] = []
            update_params: list[object] = []

            for k, col in allowed.items():
                if k in obj and obj[k] is not None:
                    sets.append(f"{col} = ?")
                    update_params.append(obj[k])

            if not sets:
                raise ValueError("No updatable fields provided.")

            # Validate date logic if either date is changing.
            if ("start_date" in obj) or ("end_date" in obj):
                existing = conn.execute(
                    "SELECT start_date, end_date FROM commitments WHERE id = ?",
                    (cid,),
                ).fetchone()
                if not existing:
                    raise ValueError(f"Commitment id={cid} not found.")

                start_date = str(obj.get("start_date") or existing["start_date"])
                end_date = str(obj.get("end_date") or existing["end_date"])

                sd = _parse_date(start_date)
                ed = _parse_date(end_date)
                if ed < sd:
                    raise ValueError("end_date must be >= start_date")

            sets.append("updated_at = ?")
            update_params.append(now)
            update_params.append(cid)

            conn.execute(
                f"UPDATE commitments SET {', '.join(sets)} WHERE id = ?",
                tuple(update_params),
            )
            conn.commit()

            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
            if not row:
                raise ValueError(f"Commitment id={cid} not found.")
            return {
                "ok": True,
                "commitment": _commitment_payload(_row_to_commitment(row)),
            }

        if op == "cancel":
            cid = _require_int(obj, "commitment_id")
            cur = conn.execute(
                """
                UPDATE commitments
                SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, cid),
            )
            conn.commit()
            if cur.rowcount == 0:
                # Could be not found or already not active.
                row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
                if not row:
                    raise ValueError(f"Commitment id={cid} not found.")
                return {
                    "ok": True,
                    "commitment": _commitment_payload(_row_to_commitment(row)),
                }
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
            return {
                "ok": True,
                "commitment": _commitment_payload(_row_to_commitment(row)),
            }

        # op == "list"
        where = []
        params: list[str] = []

        if "user_id" in obj and obj["user_id"] is not None:
            where.append("user_id = ?")
            params.append(str(obj["user_id"]))
        if "channel_id" in obj and obj["channel_id"] is not None:
            where.append("channel_id = ?")
            params.append(str(obj["channel_id"]))
        if "status" in obj and obj["status"] is not None:
            where.append("status = ?")
            params.append(str(obj["status"]))

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM commitments {clause} ORDER BY id DESC",
            tuple(params),
        ).fetchall()

        return {
            "ok": True,
            "commitments": [_commitment_payload(_row_to_commitment(r)) for r in rows],
        }

    return await _with_db(ctx, _logic)


commitment_manage_tool: ToolDef = ToolDef(
    name="commitment_manage",
    function=lambda ctx, obj: commitment_manage(ctx, obj),
    schema={
        "name": "commitment_manage",
        "description": (
            "Create/update/cancel/list a commitment for a Discord user by id. "
            "Stores the channel id so RoutineTask can post check-ins every 5 days."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "update", "cancel", "list"],
                    "description": "What to do.",
                },
                "commitment_id": {
                    "type": "integer",
                    "description": "Required for update/cancel.",
                },
                "name": {"type": "string"},
                "description": {"type": "string"},
                "user_id": {"type": "string", "description": "Discord user id."},
                "channel_id": {"type": "string", "description": "Discord channel id."},
                "start_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD. Defaults to today.",
                },
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
                "interval_days": {"type": "integer", "description": "Defaults to 5."},
                "status": {"type": "string", "enum": ["active", "cancelled", "ended"]},
                # list filters
                "channel_id": {"type": "string"},
                "user_id": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["operation"],
        },
    },
)


# ----------------------------
# RoutineTask: check-in
# ----------------------------


def _should_checkin(today: dt.date, last_checkin: str | None, interval_days: int) -> bool:
    # return True
    if not last_checkin:
        return True
    try:
        lcd = dt.date.fromisoformat(last_checkin)
    except Exception:
        return True
    return (today - lcd).days >= interval_days


async def commitment_checkin_task(ctx: GlobalContext, _obj: JSON) -> JSONDict:
    """
    Runs periodically. For each channel, pings users who have commitments due for check-in.
    """
    # print("commitment_checkin_task running...")

    if ctx.send_discord_message is None:
        raise RuntimeError("Discord send function not initialized.")
    send_fn = ctx.send_discord_message

    today = dt.date.today()
    today_s = today.isoformat()
    now = _now_ms()
    dbfile = _db_path(ctx)
    _LOGGER.debug("commitment_checkin_task starting (db=%s)", dbfile)

    async def _work(conn: sqlite3.Connection) -> JSONDict:
        # 1) End commitments past end_date
        conn.execute(
            """
            UPDATE commitments
            SET status = 'ended', updated_at = ?
            WHERE status = 'active' AND end_date < ?
            """,
            (now, today_s),
        )

        # 2) Fetch active commitments
        rows = conn.execute(
            """
            SELECT * FROM commitments
            WHERE status = 'active' AND ((start_date <= ? AND end_date >= ?) OR 1 = 1)
            ORDER BY channel_id, user_id, id
            """,
            (today_s, today_s),
        ).fetchall()

        # print(f"Found {len(rows)} active commitments.")

        due_by_channel: dict[str, list[Commitment]] = {}

        for r in rows:
            try:
                c = _row_to_commitment(r)
            except Exception:
                _LOGGER.error(
                    "Failed to parse commitment row; skipping.",
                    exc_info=True,
                )
                continue
            if _should_checkin(today, c.last_checkin_date, c.interval_days):
                due_by_channel.setdefault(c.channel_id, []).append(c)

        # 3) Send one message per channel
        due_ids: list[int] = []
        channels_pinged = 0
        channels_failed = 0
        for channel_id, commitments in due_by_channel.items():
            # Group by user
            by_user: dict[str, list[Commitment]] = {}
            for c in commitments:
                by_user.setdefault(c.user_id, []).append(c)

            mentioned = " ".join(_mention(uid) for uid in sorted(by_user.keys()))
            lines: list[str] = []
            lines.append(f"Progress check-in time.")

            for uid in sorted(by_user.keys()):
                lines.append(f"{_mention(uid)}")
                for c in by_user[uid]:
                    lines.append(f"- **{c.name}** (ends {c.end_date}): {c.description}")
            lines.append("")

            lines.append("Reply with: what you did, what’s blocked, and what you’ll do next.")

            try:
                await send_fn(channel_id, "\n".join(lines).strip())
            except Exception:
                channels_failed += 1
                _LOGGER.error(
                    "Failed to send commitment check-in to channel %s (commitments=%d users=%d).",
                    channel_id,
                    len(commitments),
                    len(by_user),
                    exc_info=True,
                )
                continue
            channels_pinged += 1
            due_ids.extend(c.id for c in commitments)

        # 4) Mark last_checkin_date for what we pinged
        if due_ids:
            qmarks = ",".join(["?"] * len(due_ids))
            conn.execute(
                f"""
                UPDATE commitments
                SET last_checkin_date = ?, updated_at = ?
                WHERE id IN ({qmarks})
                """,
                (today_s, now, *due_ids),
            )

        conn.commit()
        commitments_pinged = len(due_ids)
        if channels_pinged or channels_failed:
            _LOGGER.info(
                "Commitment check-in result: channels_pinged=%d commitments_pinged=%d channels_failed=%d",
                channels_pinged,
                commitments_pinged,
                channels_failed,
            )
        else:
            _LOGGER.debug(
                "Commitment check-in result: no commitments due (active=%d)",
                len(rows),
            )
        return {
            "ok": True,
            "channels_pinged": channels_pinged,
            "commitments_pinged": commitments_pinged,
            "channels_failed": channels_failed,
        }

    # Run in thread (sqlite is sync)
    return await _with_db(ctx, lambda conn: asyncio.run(_work(conn)))


# NOTE: RoutineTask.function signature is AsyncToolCallback(ctx, obj).
commitment_checkin_routine: RoutineTask = RoutineTask(
    name="commitment_checkin",
    description=f"Every so often, asks users about commitment progress (interval {DEFAULT_INTERVAL_DAYS} days).",
    run_every_seconds=60,  # hourly; actual gating is per-commitment interval_days
    function=lambda ctx, obj: commitment_checkin_task(ctx, obj),
)
