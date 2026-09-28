"""Tool registry: actions the assistant may take.

Rules:
- Only tools registered here can ever run: the LLM cannot invent new ones.
- Tools with requires_confirmation=True never run straight from the LLM. The call is
  parked as a pending action, the assistant asks "Should I ...?", and it runs only when
  the user's NEXT spoken words are a plain "yes" (checked by rules in intents.py, not by
  the LLM). So neither the model nor text from a web page can confirm an action.

Example (home devices, see home.py):

    register(Tool(
        name="light_off",
        description="Turn off the living room light",
        parameters={"type": "object", "properties": {}},
        func=lambda: home_assistant.call("light.turn_off", "light.living_room"),
    ))
"""
import time
from dataclasses import dataclass
from typing import Callable

CONFIRM_WINDOW_SEC = 120  # a "yes" later than this doesn't count


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema of the arguments
    func: Callable[..., str]
    requires_confirmation: bool = True
    # How the confirmation question describes the action, e.g. "{action} the {device}".
    # Filled with the call's arguments; the tool name is used when empty.
    confirm_text: str = ""
    # Per call instead of always: e.g. a light needs no "yes", the air fryer does.
    confirm_if: Callable[[dict], bool] | None = None

    def needs_confirmation(self, arguments: dict) -> bool:
        if self.confirm_if is not None:
            try:
                return bool(self.confirm_if(arguments))
            except Exception:
                return True  # when in doubt, ask
        return self.requires_confirmation


@dataclass
class PendingAction:
    name: str
    arguments: dict
    created: float

    def describe(self) -> str:
        tool = REGISTRY.get(self.name)
        words = {k: str(v).replace("_", " ") for k, v in self.arguments.items()}
        try:
            return (tool.confirm_text or "").format(**words) or self.name
        except (KeyError, IndexError, ValueError, AttributeError):
            return self.name


REGISTRY: dict[str, Tool] = {}
pending: PendingAction | None = None  # one action waiting for the user's "yes"


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
    global pending
    tool = REGISTRY.get(name)
    if tool is None:
        return f"Unknown tool '{name}'."
    if not isinstance(arguments, dict):
        arguments = {}
    if not confirmed and tool.needs_confirmation(arguments):
        pending = PendingAction(name, arguments, time.time())
        return (f"Not done yet: this needs the user's spoken yes. Ask ONE short question, "
                f"e.g. \"Should I {pending.describe()}?\", and stop. It runs only if the "
                "user answers yes.")
    try:
        return str(tool.func(**arguments))
    except Exception as e:
        return f"Tool '{name}' failed: {e}"


def waiting() -> PendingAction | None:
    """The action waiting for a yes, if it is still fresh."""
    global pending
    if pending and time.time() - pending.created > CONFIRM_WINDOW_SEC:
        pending = None
    return pending


def confirm() -> tuple[bool, str]:
    """The user said yes: run the waiting action. (ok, result text)."""
    global pending
    action, pending = waiting(), None
    if action is None:
        return False, "Nothing is waiting for confirmation."
    result = run(action.name, action.arguments, confirmed=True)
    return not result.startswith(("Tool '", "Unknown tool")), result


def cancel() -> None:
    global pending
    pending = None


# --- pictures a tool fetched (a camera snapshot) for the model to look at ----------------
_images: list[tuple[str, str]] = []  # (note, base64 JPEG); used once, never stored


def attach_image(jpeg_b64: str, note: str) -> None:
    _images.append((note, jpeg_b64))


def take_images() -> list[tuple[str, str]]:
    found = list(_images)
    _images.clear()
    return found
