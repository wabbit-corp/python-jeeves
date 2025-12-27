from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from servant.defs import RoutineTask, ToolDef, GlobalContext
from servant.json import JSON, JSONDict

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = (
    "Commitments module: create/update/cancel user commitments and periodically "
    "check-in with users in the channel they committed in."
)

DEFAULT_INTERVAL_DAYS = 5
DEFAULT_DB_FILENAME = "servant_commitments.sqlite3"


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
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / DEFAULT_DB_FILENAME).resolve()


def _connect(dbfile: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


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
            created_at TEXT NOT NULL,               -- ISO timestamp
            updated_at TEXT NOT NULL                -- ISO timestamp
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_commitments_active_channel "
        "ON commitments(status, channel_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_commitments_user "
        "ON commitments(user_id);"
    )
    conn.commit()


def _today_yyyy_mm_dd() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


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
    last_checkin_date: Optional[str]
    status: str


def _row_to_commitment(r: sqlite3.Row) -> Commitment:
    return Commitment(
        id=int(r["id"]),
        name=str(r["name"]),
        description=str(r["description"]),
        user_id=str(r["user_id"]),
        channel_id=str(r["channel_id"]),
        start_date=str(r["start_date"]),
        end_date=str(r["end_date"]),
        interval_days=int(r["interval_days"]),
        last_checkin_date=r["last_checkin_date"],
        status=str(r["status"]),
    )


async def _with_db(ctx: GlobalContext, fn):
    dbfile = _db_path(ctx)

    def _run():
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
        now = _now_iso()

        if op == "create":
            name = str(obj["name"])
            description = str(obj["description"])
            user_id = str(obj["user_id"])
            channel_id = str(obj["channel_id"])
            start_date = str(obj.get("start_date") or _today_yyyy_mm_dd())
            end_date = str(obj["end_date"]) if "end_date" in obj else None
            interval_days = int(obj.get("interval_days") or DEFAULT_INTERVAL_DAYS)

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
            cid = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
            return {"ok": True, "commitment": asdict(_row_to_commitment(row))}

        if op == "update":
            cid = int(obj["commitment_id"])

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

            sets: List[str] = []
            params: List[Any] = []

            for k, col in allowed.items():
                if k in obj and obj[k] is not None:
                    sets.append(f"{col} = ?")
                    params.append(obj[k])

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
            params.append(now)
            params.append(cid)

            conn.execute(
                f"UPDATE commitments SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()

            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
            if not row:
                raise ValueError(f"Commitment id={cid} not found.")
            return {"ok": True, "commitment": asdict(_row_to_commitment(row))}

        if op == "cancel":
            cid = int(obj["commitment_id"])
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
                return {"ok": True, "commitment": asdict(_row_to_commitment(row))}
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,)).fetchone()
            return {"ok": True, "commitment": asdict(_row_to_commitment(row))}

        # op == "list"
        where = []
        params = []

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

        return {"ok": True, "commitments": [asdict(_row_to_commitment(r)) for r in rows]}

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
                "start_date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
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

def _should_checkin(today: dt.date, last_checkin: Optional[str], interval_days: int) -> bool:
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

    today = dt.date.today()
    today_s = today.isoformat()
    now = _now_iso()

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

        due_by_channel: Dict[str, List[Commitment]] = {}
        due_ids: List[int] = []

        for r in rows:
            c = _row_to_commitment(r)
            # print(c)
            if _should_checkin(today, c.last_checkin_date, c.interval_days):
                due_by_channel.setdefault(c.channel_id, []).append(c)
                due_ids.append(c.id)

        # 3) Send one message per channel
        for channel_id, commitments in due_by_channel.items():
            # Group by user
            by_user: Dict[str, List[Commitment]] = {}
            for c in commitments:
                by_user.setdefault(c.user_id, []).append(c)

            mentioned = " ".join(_mention(uid) for uid in sorted(by_user.keys()))
            lines: List[str] = []
            lines.append(f"Progress check-in time.")

            for uid in sorted(by_user.keys()):
                lines.append(f"{_mention(uid)}")
                for c in by_user[uid]:
                    lines.append(f"- **{c.name}** (ends {c.end_date}): {c.description}")
                lines.append("")

            lines.append("Reply with: what you did, what’s blocked, and what you’ll do next.")

            await ctx.send_discord_message(channel_id, "\n".join(lines).strip())

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
        return {"ok": True, "channels_pinged": len(due_by_channel), "commitments_pinged": len(due_ids)}

    # Run in thread (sqlite is sync)
    return await _with_db(ctx, lambda conn: asyncio.run(_work(conn)))


# NOTE: RoutineTask.function signature is AsyncToolCallback(ctx, obj).
commitment_checkin_routine: RoutineTask = RoutineTask(
    name="commitment_checkin",
    description=f"Every so often, asks users about commitment progress (interval {DEFAULT_INTERVAL_DAYS} days).",
    run_every_seconds=60,  # hourly; actual gating is per-commitment interval_days
    function=lambda ctx, obj: commitment_checkin_task(ctx, obj),
)
