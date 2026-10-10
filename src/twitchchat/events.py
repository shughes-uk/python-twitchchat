"""Typed event dataclasses built from Twitch IRC messages."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

lazy from .irc import IrcMessage

__all__ = [
    "ChatMessage",
    "ClearChat",
    "ClearMsg",
    "Connected",
    "Event",
    "GlobalUserState",
    "Join",
    "Notice",
    "Part",
    "RawEvent",
    "Reconnect",
    "RoomState",
    "UserNotice",
    "UserState",
    "Whisper",
    "event_from_irc",
    "parse_badges",
]

_ACTION_PREFIX = "\x01ACTION "


def parse_badges(value: str | None) -> dict[str, str]:
    """Parse a ``badges``/``badge-info`` tag (``subscriber/12,premium/1``)."""
    if not value:
        return {}
    badges: dict[str, str] = {}
    for item in value.split(","):
        name, _, version = item.partition("/")
        if name:
            badges[name] = version
    return badges


def _timestamp(tags: dict[str, str]) -> datetime | None:
    ts = tags.get("tmi-sent-ts")
    if ts and ts.isascii() and ts.isdigit():
        try:
            return datetime.fromtimestamp(int(ts) / 1000, tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
    return None


def _int(value: str | None) -> int | None:
    digits = "" if value is None else value.removeprefix("-")
    if value is None or not (digits.isascii() and digits.isdigit()):
        return None
    try:
        return int(value)
    except ValueError:  # non-numeric, or longer than int's max string digits
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Base class for every event. ``raw`` is the underlying parsed IRC line."""

    raw: IrcMessage = field(repr=False)

    @property
    def tags(self) -> dict[str, str]:
        return self.raw.tags


@dataclass(frozen=True, slots=True, kw_only=True)
class Connected(Event):
    """Successfully authenticated (``001`` welcome). Emitted on every (re)connect."""

    nick: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMessage(Event):
    """A chat message in a channel (``PRIVMSG``)."""

    channel: str
    login: str
    text: str
    display_name: str
    id: str | None = None
    user_id: str | None = None
    room_id: str | None = None
    color: str | None = None
    badges: dict[str, str] = field(default_factory=dict)
    is_action: bool = False
    bits: int | None = None
    timestamp: datetime | None = None

    @property
    def is_mod(self) -> bool:
        return self.tags.get("mod") == "1" or "broadcaster" in self.badges

    @property
    def is_subscriber(self) -> bool:
        return self.tags.get("subscriber") == "1"

    @property
    def is_first_message(self) -> bool:
        return self.tags.get("first-msg") == "1"

    @property
    def reply_parent_id(self) -> str | None:
        return self.tags.get("reply-parent-msg-id")


@dataclass(frozen=True, slots=True, kw_only=True)
class UserNotice(Event):
    """Subs, resubs, gift subs, raids, announcements ... (``USERNOTICE``).

    ``msg_id`` is the kind (``sub``, ``resub``, ``subgift``, ``raid``,
    ``announcement`` ...), ``params`` holds the ``msg-param-*`` tags with the
    prefix stripped, and ``text`` is the optional user-supplied message.
    """

    channel: str
    msg_id: str
    login: str | None
    display_name: str | None
    system_msg: str
    text: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Join(Event):
    """A user joined a channel (requires the ``twitch.tv/membership`` capability)."""

    channel: str
    login: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Part(Event):
    """A user left a channel."""

    channel: str
    login: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Notice(Event):
    """A server notice. ``channel`` is ``None`` for global notices."""

    channel: str | None
    msg_id: str | None
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ClearChat(Event):
    """Chat cleared (``login`` is ``None``) or a user banned/timed out."""

    channel: str
    login: str | None
    ban_duration: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClearMsg(Event):
    """A single message was deleted."""

    channel: str
    login: str | None
    target_msg_id: str | None
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomState(Event):
    """Channel chat settings (emote-only, slow mode, ...). Inspect ``tags``."""

    channel: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UserState(Event):
    """Your own state in a channel (sent on join and after you send a message)."""

    channel: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GlobalUserState(Event):
    """Your own global state, sent after authenticating with a real token."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Whisper(Event):
    """A whisper (private message) to the authenticated user."""

    login: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Reconnect(Event):
    """Twitch is about to drop the connection; the client reconnects automatically."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RawEvent(Event):
    """Any other IRC message (numerics, CAP ACK, ...) not mapped to a typed event."""


def _nick(msg: IrcMessage) -> str:
    return msg.prefix.nick if msg.prefix else ""


def _chat_message(msg: IrcMessage) -> ChatMessage:
    tags = msg.tags
    text = msg.trailing or ""
    is_action = text.startswith(_ACTION_PREFIX) and text.endswith("\x01")
    if is_action:
        text = text[len(_ACTION_PREFIX) : -1]
    login = _nick(msg)
    return ChatMessage(
        raw=msg,
        channel=msg.channel or "",
        login=login,
        text=text,
        display_name=tags.get("display-name") or login,
        id=tags.get("id"),
        user_id=tags.get("user-id"),
        room_id=tags.get("room-id"),
        color=tags.get("color") or None,
        badges=parse_badges(tags.get("badges")),
        is_action=is_action,
        bits=_int(tags.get("bits")),
        timestamp=_timestamp(tags),
    )


def _user_notice(msg: IrcMessage) -> UserNotice:
    tags = msg.tags
    prefix = "msg-param-"
    return UserNotice(
        raw=msg,
        channel=msg.channel or "",
        msg_id=tags.get("msg-id", ""),
        login=tags.get("login"),
        display_name=tags.get("display-name"),
        system_msg=tags.get("system-msg", ""),
        text=msg.params[1] if len(msg.params) > 1 else None,
        params={k.removeprefix(prefix): v for k, v in tags.items() if k.startswith(prefix)},
        timestamp=_timestamp(tags),
    )


def event_from_irc(msg: IrcMessage) -> Event:  # noqa: PLR0911, PLR0912
    """Convert a parsed IRC message into the most specific :class:`Event`."""
    channel = msg.channel
    match msg.command:
        case "PRIVMSG" if channel is not None:
            return _chat_message(msg)
        case "USERNOTICE" if channel is not None:
            return _user_notice(msg)
        case "JOIN" if channel is not None:
            return Join(raw=msg, channel=channel, login=_nick(msg))
        case "PART" if channel is not None:
            return Part(raw=msg, channel=channel, login=_nick(msg))
        case "NOTICE":
            return Notice(
                raw=msg, channel=channel, msg_id=msg.tags.get("msg-id"), text=msg.trailing or ""
            )
        case "CLEARCHAT" if channel is not None:
            return ClearChat(
                raw=msg,
                channel=channel,
                login=msg.params[1] if len(msg.params) > 1 else None,
                ban_duration=_int(msg.tags.get("ban-duration")),
            )
        case "CLEARMSG" if channel is not None:
            return ClearMsg(
                raw=msg,
                channel=channel,
                login=msg.tags.get("login"),
                target_msg_id=msg.tags.get("target-msg-id"),
                text=msg.params[1] if len(msg.params) > 1 else "",
            )
        case "ROOMSTATE" if channel is not None:
            return RoomState(raw=msg, channel=channel)
        case "USERSTATE" if channel is not None:
            return UserState(raw=msg, channel=channel)
        case "GLOBALUSERSTATE":
            return GlobalUserState(raw=msg)
        case "WHISPER":
            return Whisper(raw=msg, login=_nick(msg), text=msg.trailing or "")
        case "RECONNECT":
            return Reconnect(raw=msg)
        case "001":
            return Connected(raw=msg, nick=msg.params[0] if msg.params else "")
        case _:
            return RawEvent(raw=msg)
