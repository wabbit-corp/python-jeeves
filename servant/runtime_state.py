"""Shared runtime state without imports from feature implementations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field


@dataclass
class ChannelControlsState:
    disabled: dict[str, str] = field(default_factory=dict)
    loaded: bool = False


@dataclass
class GuildRetentionState:
    removed_guilds: set[str] = field(default_factory=set)
    removed_channels: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    transcript_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class PrivacyState:
    opted_out_users: set[str] = field(default_factory=set)
    pending_opt_outs: set[str] = field(default_factory=set)
    generation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    processing_tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def is_opted_out(self, user_id: str | int) -> bool:
        return str(user_id) in self.opted_out_users
