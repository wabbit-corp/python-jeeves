from typing import Any, Dict, Callable, List, Awaitable
from dataclasses import dataclass

from servant.base.json import JSON

# async def foo(data: JSON) -> Any:
AsyncToolCallback = Callable[[JSON], Awaitable[Any]]

@dataclass
class ToolDef:
    name: str
    schema: JSON
    function: AsyncToolCallback


@dataclass
class ToolDispatcher:
    tools: Dict[str, ToolDef]

    @property
    def schema(self) -> List[JSON]:
        return [tool.schema for name, tool in self.tools.items()]

    async def dispatch(self, tool_name: str, data: JSON) -> Any:
        tool = self.tools[tool_name]
        return await tool.function(data)

    def register(self, name: str, schema: Dict[str, Any], function: AsyncToolCallback) -> None:
        self.tools[name] = ToolDef(name=name, schema=schema, function=function)
