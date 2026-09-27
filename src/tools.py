"""Tool registry: actions the assistant may take (empty for now).

Later this is where home devices go (lights, TV, washer...). Rules:
- Only tools registered here can ever run: the LLM cannot invent new ones.
- Tools with requires_confirmation=True never run straight from the LLM. The
  assistant must first ask "Should I ...?" and the user must say yes.

Example for later:

    register(Tool(
        name="light_off",
        description="Turn off the living room light",
        parameters={"type": "object", "properties": {}},
        func=lambda: home_assistant.call("light.turn_off", "light.living_room"),
    ))
"""
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema of the arguments
    func: Callable[..., str]
    requires_confirmation: bool = True


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    REGISTRY[tool.name] = tool


def ollama_specs() -> list[dict]:
    """Tool descriptions in the format Ollama's /api/chat expects."""
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in REGISTRY.values()
    ]


def run(name: str, arguments: dict, confirmed: bool = False) -> str:
    tool = REGISTRY.get(name)
    if tool is None:
        return f"Unknown tool '{name}'."
    if tool.requires_confirmation and not confirmed:
        return f"Not done: '{name}' needs the user's spoken confirmation first. Ask them."
    try:
        return str(tool.func(**arguments))
    except Exception as e:
        return f"Tool '{name}' failed: {e}"
