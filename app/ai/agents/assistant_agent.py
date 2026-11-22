from __future__ import annotations

from typing import TYPE_CHECKING

from app.ai.tools.system_status_tool import SystemStatusTool
from swiftpy.ai.agents import AgentMemory, BaseAgent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swiftpy.ai.agents import BaseMemoryBackend
    from swiftpy.ai.client import AIClient
    from swiftpy.ai.tools import BaseTool
    from swiftpy.core.config import Config


class AssistantAgent(BaseAgent):
    """Default conversational assistant agent wired with starter tools."""

    def __init__(
        self,
        *,
        model: str | None = None,
        system_prompt: str = (
            "You are a helpful, concise SwiftPY application assistant."
        ),
        tools: Sequence[BaseTool | type[BaseTool]] | None = None,
        memory: AgentMemory | BaseMemoryBackend | None = None,
        client: AIClient | None = None,
        config: Config | None = None,
    ) -> None:
        default_tools: list[BaseTool | type[BaseTool]] = (
            list(tools) if tools is not None else [SystemStatusTool()]
        )
        super().__init__(
            name="AssistantAgent",
            model=model,
            system_prompt=system_prompt,
            tools=default_tools,
            memory=memory if memory is not None else AgentMemory(),
            client=client,
            config=config,
        )
