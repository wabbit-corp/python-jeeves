from __future__ import annotations

import asyncio
import csv
import datetime as dt
import logging
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import discord

from servant import permissions
from servant.defs import GlobalContext, ToolDef
from servant.modules import commitment, topic_subscriptions
from typed_json import JSON, JSONDict, obj_to_json

_LOGGER = logging.getLogger(__name__)

MODULE_PROMPT = "DB export module: export topic subscriptions and commitments data to CSV and zip."


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _format_csv_value(value: object) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return "hex:" + value.hex()
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _export_db_to_csv(db_path: Path, out_dir: Path, *, prefix: str) -> tuple[list[JSONDict], list[Path]]:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA query_only=1;")

    exports: list[JSONDict] = []
    csv_paths: list[Path] = []
    try:
        tables = _list_tables(conn)
        for table in tables:
            columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})")]
            csv_path = out_dir / f"{prefix}_{table}.csv"
            row_count = 0

            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if columns:
                    writer.writerow(columns)
                cursor = conn.execute(f"SELECT * FROM {_quote_ident(table)}")
                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    for row in batch:
                        row_count += 1
                        if columns:
                            writer.writerow([_format_csv_value(row[col]) for col in columns])
                        else:
                            writer.writerow([_format_csv_value(value) for value in row])

            exports.append(
                {
                    "database": prefix,
                    "table": table,
                    "rows": row_count,
                    "csv": csv_path.name,
                }
            )
            csv_paths.append(csv_path)
    finally:
        conn.close()

    return exports, csv_paths


def _build_zip(
    out_dir: Path,
    zip_name: str,
    *,
    subscriptions_db: Path,
    commitments_db: Path,
) -> tuple[Path, list[JSONDict]]:
    exports: list[JSONDict] = []
    csv_paths: list[Path] = []

    subscription_exports, subscription_paths = _export_db_to_csv(
        subscriptions_db, out_dir, prefix="topic_subscriptions"
    )
    exports.extend(subscription_exports)
    csv_paths.extend(subscription_paths)

    commitment_exports, commitment_paths = _export_db_to_csv(commitments_db, out_dir, prefix="commitments")
    exports.extend(commitment_exports)
    csv_paths.extend(commitment_paths)

    zip_path = out_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for csv_path in csv_paths:
            handle.write(csv_path, arcname=csv_path.name)

    return zip_path, exports


async def _send_file(
    ctx: GlobalContext,
    channel_id: str,
    file_path: Path,
    *,
    filename: str,
) -> None:
    client = getattr(ctx, "discord_client", None)
    if client is None:
        raise RuntimeError("Discord client not initialized yet.")

    loop = getattr(ctx, "discord_loop", None)
    if loop is None:
        raise RuntimeError("ctx.discord_loop not set yet (client not initialized).")

    async def _send() -> None:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        await channel.send(file=discord.File(str(file_path), filename=filename))

    try:
        running = asyncio.get_running_loop()
        if running is loop:
            await _send()
            return
    except RuntimeError:
        running = None

    fut = asyncio.run_coroutine_threadsafe(_send(), loop)
    if running is not None:
        await asyncio.wrap_future(fut)
    else:
        fut.result()


async def export_subscriptions_commitments_zip(ctx: GlobalContext, obj: JSON) -> JSONDict:
    if not isinstance(obj, dict):
        raise ValueError("Input must be an object.")

    channel_id = obj.get("channel_id")
    if not channel_id:
        raise ValueError("channel_id is required.")
    channel_id = str(channel_id)

    await permissions.require_admin_for_channel(ctx, channel_id, "export databases")

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_name = f"servant_db_export_{timestamp}.zip"

    subscriptions_db = topic_subscriptions._db_path(ctx)
    commitments_db = commitment._db_path(ctx)

    with tempfile.TemporaryDirectory(prefix="servant_db_export_") as tmpdir:
        out_dir = Path(tmpdir)
        zip_path, exports = await asyncio.to_thread(
            _build_zip,
            out_dir,
            zip_name,
            subscriptions_db=subscriptions_db,
            commitments_db=commitments_db,
        )

        await _send_file(ctx, channel_id, zip_path, filename=zip_name)

    return {
        "ok": True,
        "zip_filename": zip_name,
        "tables": obj_to_json(exports),
        "channel_id": channel_id,
    }


export_subscriptions_commitments_zip_tool: ToolDef = ToolDef(
    name="export_subscriptions_commitments_zip",
    function=lambda ctx, obj: export_subscriptions_commitments_zip(ctx, obj),
    schema={
        "name": "export_subscriptions_commitments_zip",
        "description": (
            "Export topic subscriptions and commitments databases to CSV files, "
            "bundle them into a zip, and upload the zip to the provided Discord channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel id where the zip should be uploaded.",
                },
            },
            "required": ["channel_id"],
        },
    },
)
