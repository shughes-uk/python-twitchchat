"""Asyncio client for Twitch chat over IRC."""

from importlib.metadata import PackageNotFoundError, version

from .client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    AuthenticationError,
    EventStream,
    Listener,
    RateLimiter,
    TwitchChat,
    TwitchChatError,
)
from .events import (
    ChatMessage,
    ClearChat,
    ClearMsg,
    Connected,
    Event,
    GlobalUserState,
    Join,
    Notice,
    Part,
    RawEvent,
    Reconnect,
    RoomState,
    UserNotice,
    UserState,
    Whisper,
)
from .irc import IrcMessage, Prefix, format_line, parse_line

try:
    __version__ = version("python-twitchchat")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AuthenticationError",
    "ChatMessage",
    "ClearChat",
    "ClearMsg",
    "Connected",
    "Event",
    "EventStream",
    "GlobalUserState",
    "IrcMessage",
    "Join",
    "Listener",
    "Notice",
    "Part",
    "Prefix",
    "RateLimiter",
    "RawEvent",
    "Reconnect",
    "RoomState",
    "TwitchChat",
    "TwitchChatError",
    "UserNotice",
    "UserState",
    "Whisper",
    "__version__",
    "format_line",
    "parse_line",
]
