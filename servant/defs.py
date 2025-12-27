from __future__ import annotations
from typing import Callable, Awaitable, Dict, Any, List
from dataclasses import dataclass, field
from servant.json import JSON, JSONDict
from abc import ABC, abstractmethod
from pathlib import Path
import logging
from collections import defaultdict
from openai import AsyncOpenAI

_LOGGER = logging.getLogger(__name__)


AsyncToolCallback = Callable[["GlobalContext", JSON], Awaitable[Any]]

SECRET_OPENAI_KEY = "openai-key"
SECRET_USER_AGENT = "user-agent"
SECRET_DISCORD_TOKEN = "discord-token"
SECRET_IMGFLIP_USERNAME = "imgflip-username"
SECRET_IMGFLIP_PASSWORD = "imgflip-password"
SECRET_BRAVE_KEY = "brave-key"


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
class Module:
    name: str
    module_prompt: str | None = None
    tools: Dict[str, ToolDef] = field(default_factory=dict)
    personalities: Dict[str, Personality] = field(default_factory=dict)
    routine_tasks: Dict[str, RoutineTask] = field(default_factory=dict)


@dataclass
class GlobalContext:
    openai_client: AsyncOpenAI = None  # type: ignore
    secrets: Dict[str, Any] = field(default_factory=dict)
    modules: Dict[str, Module] = field(default_factory=dict)
    channel_messages: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    channel_personality: Dict[str, Personality] = field(default_factory=dict)


def discover_modules() -> Dict[str, Module]:
    module_dir = Path(__file__).parent / "modules"

    modules = {}
    for subfile in module_dir.iterdir():
        module_name = subfile.stem

        if module_name.startswith("_"):
            _LOGGER.debug(f"Skipping private module: {module_name}")
            continue

        try:
            if subfile.is_file() and subfile.suffix == ".py":
                module = __import__(
                    f"servant.modules.{module_name}", fromlist=[module_name]
                )
            elif subfile.is_dir():
                module = __import__(
                    f"servant.modules.{module_name}", fromlist=[module_name]
                )
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
                        modules[module_name] = Module(
                            name=module_name, module_prompt=module_prompt
                        )
                    modules[module_name].tools[attr.name] = attr
                elif isinstance(attr, Personality):
                    if module_name not in modules:
                        modules[module_name] = Module(
                            name=module_name, module_prompt=module_prompt
                        )
                    modules[module_name].personalities[attr.name] = attr
                elif isinstance(attr, RoutineTask):
                    if module_name not in modules:
                        modules[module_name] = Module(
                            name=module_name, module_prompt=module_prompt
                        )
                    modules[module_name].routine_tasks[attr.name] = attr

            if module_name not in modules and module_prompt is not None:
                modules[module_name] = Module(
                    name=module_name, module_prompt=module_prompt
                )

            if module_name not in modules:
                _LOGGER.warning(
                    f"Module {module_name} does not define any tools or personalities."
                )
                continue

        except Exception as e:
            _LOGGER.error(f"Failed to import module {module_name}: {e}")
            _LOGGER.exception(e)
            continue

    return modules
