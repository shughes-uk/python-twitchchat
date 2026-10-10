"""Low-level parsing and formatting of Twitch IRC (IRCv3 with tags) lines."""

from dataclasses import dataclass, field

__all__ = [
    "IrcMessage",
    "Prefix",
    "escape_tag_value",
    "format_line",
    "parse_line",
    "parse_tags",
    "unescape_tag_value",
]

_UNESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
_ESCAPES = {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}


def unescape_tag_value(value: str) -> str:
    r"""Unescape an IRCv3 tag value (``\s`` -> space, ``\:`` -> ``;`` ...).

    Unknown escapes drop the backslash; a trailing lone backslash is removed,
    as required by the IRCv3 message-tags specification.
    """
    if "\\" not in value:
        return value
    out: list[str] = []
    chars = iter(value)
    for ch in chars:
        if ch != "\\":
            out.append(ch)
            continue
        nxt = next(chars, None)
        if nxt is None:
            break
        out.append(_UNESCAPES.get(nxt, nxt))
    return "".join(out)


def escape_tag_value(value: str) -> str:
    """Escape a string for use as an IRCv3 tag value."""
    return "".join(_ESCAPES.get(ch, ch) for ch in value)


def parse_tags(raw: str) -> dict[str, str]:
    """Parse the tag section of a line (without the leading ``@``)."""
    tags: dict[str, str] = {}
    for item in raw.split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        tags[key] = unescape_tag_value(value)
    return tags


@dataclass(frozen=True, slots=True)
class Prefix:
    """The source of an IRC message: ``nick!user@host`` or a bare server name."""

    nick: str
    user: str | None = None
    host: str | None = None

    @classmethod
    def parse(cls, raw: str) -> Prefix:
        nick, _, rest = raw.partition("!")
        if rest:
            user, _, host = rest.partition("@")
            return cls(nick, user, host or None)
        nick, _, host = nick.partition("@")
        return cls(nick, None, host or None)

    def __str__(self) -> str:
        s = self.nick
        if self.user is not None:
            s += f"!{self.user}"
        if self.host is not None:
            s += f"@{self.host}"
        return s


@dataclass(frozen=True, slots=True)
class IrcMessage:
    """A single parsed IRC line."""

    command: str
    params: tuple[str, ...] = ()
    tags: dict[str, str] = field(default_factory=dict)
    prefix: Prefix | None = None
    raw: str = ""

    @property
    def channel(self) -> str | None:
        """The channel (without ``#``) if the first parameter is a channel."""
        if self.params and self.params[0].startswith("#"):
            return self.params[0][1:]
        return None

    @property
    def trailing(self) -> str | None:
        """The last parameter, which for most commands is the message text."""
        return self.params[-1] if self.params else None


def parse_line(line: str) -> IrcMessage:
    """Parse one IRC line (with or without the trailing CRLF).

    Raises:
        ValueError: if the line contains no command.
    """
    raw = line.rstrip("\r\n")
    rest = raw
    tags: dict[str, str] = {}
    prefix: Prefix | None = None

    if rest.startswith("@"):
        tag_part, _, rest = rest[1:].partition(" ")
        tags = parse_tags(tag_part)
        rest = rest.lstrip(" ")
    if rest.startswith(":"):
        prefix_part, _, rest = rest[1:].partition(" ")
        prefix = Prefix.parse(prefix_part)
        rest = rest.lstrip(" ")

    trailing: str | None = None
    if " :" in rest:
        rest, _, trailing = rest.partition(" :")
    elif rest.startswith(":"):
        trailing = rest[1:]
        rest = ""
    parts = [p for p in rest.split(" ") if p]
    if not parts:
        msg = f"no command in IRC line: {raw!r}"
        raise ValueError(msg)
    command, *params = parts
    if trailing is not None:
        params.append(trailing)
    return IrcMessage(command.upper(), tuple(params), tags, prefix, raw)


def format_line(command: str, *params: str, tags: dict[str, str] | None = None) -> str:
    """Build an IRC line (with CRLF). The last param is sent as trailing if needed.

    Raises:
        ValueError: if any part contains CR, LF or NUL (which would allow injection).
    """
    for part in (command, *params):
        if "\r" in part or "\n" in part or "\0" in part:
            msg = "IRC parameters must not contain CR or LF (or NUL)"
            raise ValueError(msg)
    pieces: list[str] = []
    if tags:
        pieces.append("@" + ";".join(f"{k}={escape_tag_value(v)}" for k, v in tags.items()))
    pieces.append(command)
    if params:
        *middle, last = params
        for p in middle:
            if not p or " " in p or p.startswith(":"):
                msg = f"invalid middle IRC parameter: {p!r}"
                raise ValueError(msg)
        pieces.extend(middle)
        if not last or " " in last or last.startswith(":"):
            last = ":" + last
        pieces.append(last)
    return " ".join(pieces) + "\r\n"
