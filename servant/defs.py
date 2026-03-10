from __future__ import annotations

import logging
from asyncio import AbstractEventLoop
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from openai import AsyncOpenAI

from typed_json import JSON, JSONDict

if TYPE_CHECKING:
    import discord

_LOGGER = logging.getLogger(__name__)


AsyncToolCallback: TypeAlias = Callable[["GlobalContext", JSON], Awaitable[JSON]]

SECRET_OPENAI_KEY = "openai.key"
SECRET_USER_AGENT = "web.user-agent"
SECRET_DISCORD_TOKEN = "discord.token"
SECRET_IMGFLIP_USERNAME = "imgflip.username"
SECRET_IMGFLIP_PASSWORD = "imgflip.password"
SECRET_BRAVE_KEY = "brave.key"
SECRET_GITHUB_TOKEN = "github.token"
SECRET_GOOGLE_DRIVE_CLIENT_ID = "google.drive.client_id"
SECRET_GOOGLE_DRIVE_CLIENT_SECRET = "google.drive.client_secret"
SECRET_GOOGLE_DRIVE_REDIRECT_URI = "google.drive.redirect_uri"

ALL_SECRETS = [
    SECRET_OPENAI_KEY,
    SECRET_USER_AGENT,
    SECRET_DISCORD_TOKEN,
    SECRET_IMGFLIP_USERNAME,
    SECRET_IMGFLIP_PASSWORD,
    SECRET_BRAVE_KEY,
    SECRET_GITHUB_TOKEN,
    SECRET_GOOGLE_DRIVE_CLIENT_ID,
    SECRET_GOOGLE_DRIVE_CLIENT_SECRET,
    SECRET_GOOGLE_DRIVE_REDIRECT_URI,
]


@dataclass
class ToolDef:
    name: str
    schema: JSONDict
    function: AsyncToolCallback


@dataclass
class Personality:
    name: str
    description: str


@dataclass
class RoutineTask:
    name: str
    description: str
    run_every_seconds: int
    function: AsyncToolCallback


@dataclass
class RoutineTaskState:
    name: str
    description: str
    run_every_seconds: int
    function: AsyncToolCallback
    last_run_timestamp: float = 0.0
    run_count: int = 0


@dataclass
class Module:
    name: str
    module_prompt: str | None = None
    tools: dict[str, ToolDef] = field(default_factory=dict)
    personalities: dict[str, Personality] = field(default_factory=dict)
    routine_tasks: dict[str, RoutineTaskState] = field(default_factory=dict)


DiscordSendFn: TypeAlias = Callable[[str, str], Awaitable[None]]


@dataclass
class GlobalContext:
    openai_client: AsyncOpenAI | None = None
    secrets: dict[str, JSON] = field(default_factory=dict)
    modules: dict[str, Module] = field(default_factory=dict)
    module_state: dict[str, object] = field(default_factory=dict)
    channel_messages: dict[str, list[JSONDict]] = field(default_factory=lambda: defaultdict(list))
    channel_personality: dict[str, Personality] = field(default_factory=dict)
    current_channel_id: str | None = None
    current_user_id: str | None = None

    send_discord_message: DiscordSendFn | None = None

    discord_loop: AbstractEventLoop | None = None
    discord_client: discord.Client | None = None
    _db_initialized: bool = False
    _indexer_last_sync_ts: float = 0.0
    _indexer_last_search_ts: float = 0.0


def discover_modules() -> dict[str, Module]:
    module_dir = Path(__file__).parent / "modules"

    modules: dict[str, Module] = {}
    for subfile in module_dir.iterdir():
        module_name = subfile.stem

        if module_name.startswith("_"):
            _LOGGER.debug(f"Skipping private module: {module_name}")
            continue

        try:
            if subfile.is_file() and subfile.suffix == ".py":
                module = __import__(f"servant.modules.{module_name}", fromlist=[module_name])
            elif subfile.is_dir():
                module = __import__(f"servant.modules.{module_name}", fromlist=[module_name])
            else:
                _LOGGER.warning(f"Skipping non-Python file or directory: {subfile}")
                continue

            if hasattr(module, "MODULE_PROMPT"):
                module_prompt = module.MODULE_PROMPT
            else:
                module_prompt = None

            # List all definitions in the module
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, ToolDef):
                    if module_name not in modules:
                        modules[module_name] = Module(name=module_name, module_prompt=module_prompt)
                    modules[module_name].tools[attr.name] = attr
                elif isinstance(attr, Personality):
                    if module_name not in modules:
                        modules[module_name] = Module(name=module_name, module_prompt=module_prompt)
                    modules[module_name].personalities[attr.name] = attr
                elif isinstance(attr, RoutineTask):
                    if module_name not in modules:
                        modules[module_name] = Module(name=module_name, module_prompt=module_prompt)
                    modules[module_name].routine_tasks[attr.name] = RoutineTaskState(
                        name=attr.name,
                        description=attr.description,
                        run_every_seconds=attr.run_every_seconds,
                        function=attr.function,
                    )

            if module_name not in modules and module_prompt is not None:
                modules[module_name] = Module(name=module_name, module_prompt=module_prompt)

            if module_name not in modules:
                _LOGGER.warning(f"Module {module_name} does not define any tools or personalities.")
                continue

        except Exception as e:
            _LOGGER.error(f"Failed to import module {module_name}: {e}")
            _LOGGER.exception(e)
            continue

    return modules
