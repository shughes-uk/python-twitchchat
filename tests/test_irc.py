import pytest

from twitchchat.irc import (
    Prefix,
    escape_tag_value,
    format_line,
    parse_line,
    parse_tags,
    unescape_tag_value,
)


def test_parse_privmsg_with_tags_and_prefix() -> None:
    line = (
        "@badge-info=;badges=moderator/1;color=#1E90FF;display-name=Foo;id=abc;mod=1"
        " :foo!foo@foo.tmi.twitch.tv PRIVMSG #bar :hello there : world\r\n"
    )
    msg = parse_line(line)
    assert msg.command == "PRIVMSG"
    assert msg.params == ("#bar", "hello there : world")
    assert msg.channel == "bar"
    assert msg.trailing == "hello there : world"
    assert msg.prefix == Prefix("foo", "foo", "foo.tmi.twitch.tv")
    assert msg.tags["badge-info"] == ""
    assert msg.tags["color"] == "#1E90FF"
    assert msg.raw == line.rstrip("\r\n")


def test_parse_ping_without_prefix() -> None:
    msg = parse_line("PING :tmi.twitch.tv")
    assert msg.command == "PING"
    assert msg.prefix is None
    assert msg.tags == {}
    assert msg.params == ("tmi.twitch.tv",)


def test_parse_server_prefix_numeric() -> None:
    msg = parse_line(":tmi.twitch.tv 001 justinfan123 :Welcome, GLHF!")
    assert msg.command == "001"
    assert msg.prefix == Prefix("tmi.twitch.tv")
    assert msg.params == ("justinfan123", "Welcome, GLHF!")
    assert msg.channel is None


def test_parse_no_trailing() -> None:
    msg = parse_line(":foo!foo@foo.tmi.twitch.tv JOIN #bar")
    assert msg.params == ("#bar",)
    assert msg.channel == "bar"


def test_parse_cap_ack_multiple_middle_params() -> None:
    msg = parse_line(":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands")
    assert msg.params == ("*", "ACK", "twitch.tv/tags twitch.tv/commands")


def test_parse_lowercase_command_normalised() -> None:
    assert parse_line("ping :x").command == "PING"


@pytest.mark.parametrize("line", ["", "\r\n", "@a=b", ":prefix.only"])
def test_parse_rejects_lines_without_command(line: str) -> None:
    with pytest.raises(ValueError, match="no command"):
        parse_line(line)


def test_tag_without_value_and_empty_items() -> None:
    assert parse_tags("a;b=;c=1;;") == {"a": "", "b": "", "c": "1"}


@pytest.mark.parametrize(
    ("escaped", "plain"),
    [
        (r"hello\sworld", "hello world"),
        (r"a\:b", "a;b"),
        (r"back\\slash", "back\\slash"),
        (r"cr\rlf\n", "cr\rlf\n"),
        (r"unknown\x", "unknownx"),
        ("trailing\\", "trailing"),
        ("plain", "plain"),
        (r"\s\s", "  "),
    ],
)
def test_unescape_tag_value(escaped: str, plain: str) -> None:
    assert unescape_tag_value(escaped) == plain


def test_escape_roundtrip() -> None:
    value = "a b;c\\d\r\n"
    assert unescape_tag_value(escape_tag_value(value)) == value
    assert escape_tag_value(value) == r"a\sb\:c\\d\r\n"


def test_escaped_tags_in_line() -> None:
    msg = parse_line(r"@system-msg=5\sraiders\sfrom\sX;msg-id=raid :tmi.twitch.tv USERNOTICE #y")
    assert msg.tags["system-msg"] == "5 raiders from X"
    assert msg.params == ("#y",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("nick!user@host", Prefix("nick", "user", "host")),
        ("tmi.twitch.tv", Prefix("tmi.twitch.tv")),
        ("nick@host", Prefix("nick", None, "host")),
    ],
)
def test_prefix_parse_and_str(raw: str, expected: Prefix) -> None:
    assert Prefix.parse(raw) == expected
    assert str(expected) == raw


def test_format_line() -> None:
    assert format_line("PRIVMSG", "#chan", "hi there") == "PRIVMSG #chan :hi there\r\n"
    assert format_line("JOIN", "#chan") == "JOIN #chan\r\n"
    assert format_line("PONG", ":colon") == "PONG ::colon\r\n"
    assert format_line("PRIVMSG", "#c", "") == "PRIVMSG #c :\r\n"
    assert (
        format_line("PRIVMSG", "#c", "x", tags={"reply-parent-msg-id": "a b"})
        == "@reply-parent-msg-id=a\\sb PRIVMSG #c x\r\n"
    )


@pytest.mark.parametrize("bad", ["a\r\nQUIT", "x\n"])
def test_format_line_rejects_crlf_injection(bad: str) -> None:
    with pytest.raises(ValueError, match="CR or LF"):
        format_line("PRIVMSG", "#c", bad)


def test_format_line_rejects_bad_middle_param() -> None:
    with pytest.raises(ValueError, match="middle"):
        format_line("PRIVMSG", "#a b", "x")


def test_format_parse_roundtrip() -> None:
    line = format_line("PRIVMSG", "#chan", ":) hello", tags={"k": "v;w"})
    msg = parse_line(line)
    assert msg.params == ("#chan", ":) hello")
    assert msg.tags == {"k": "v;w"}


def test_format_line_rejects_nul() -> None:
    with pytest.raises(ValueError, match="CR or LF"):
        format_line("PRIVMSG", "#c", "a\0b")


def test_parse_splits_middle_params_on_spaces_only() -> None:
    msg = parse_line("PRIVMSG #c\tx :hi")
    assert msg.params == ("#c\tx", "hi")
