from datetime import UTC, datetime

from twitchchat import (
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
from twitchchat.events import event_from_irc, parse_badges
from twitchchat.irc import parse_line


def ev(line: str) -> Event:
    return event_from_irc(parse_line(line))


PRIVMSG = (
    "@badge-info=subscriber/14;badges=subscriber/12,premium/1;bits=100;color=;"
    "display-name=Ronni;emotes=;first-msg=1;id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;mod=0;"
    "room-id=1337;subscriber=1;tmi-sent-ts=1507246572675;turbo=0;user-id=1337;user-type="
    " :ronni!ronni@ronni.tmi.twitch.tv PRIVMSG #ronni :Kappa Keepo Kappa"
)


def test_chat_message() -> None:
    e = ev(PRIVMSG)
    assert isinstance(e, ChatMessage)
    assert e.channel == "ronni"
    assert e.login == "ronni"
    assert e.display_name == "Ronni"
    assert e.text == "Kappa Keepo Kappa"
    assert e.id == "b34ccfc7-4977-403a-8a94-33c6bac34fb8"
    assert e.user_id == e.room_id == "1337"
    assert e.color is None
    assert e.badges == {"subscriber": "12", "premium": "1"}
    assert e.bits == 100
    assert e.timestamp == datetime(2017, 10, 5, 23, 36, 12, 675000, tzinfo=UTC)
    assert e.is_subscriber
    assert not e.is_mod
    assert e.is_first_message
    assert not e.is_action
    assert e.reply_parent_id is None
    assert e.tags["turbo"] == "0"


def test_chat_message_action_and_untagged() -> None:
    e = ev(":foo!foo@foo.tmi.twitch.tv PRIVMSG #bar :\x01ACTION waves\x01")
    assert isinstance(e, ChatMessage)
    assert e.is_action
    assert e.text == "waves"
    assert e.display_name == "foo"
    assert e.timestamp is None
    assert e.badges == {}


def test_broadcaster_is_mod() -> None:
    e = ev("@badges=broadcaster/1;mod=0 :a!a@a.tmi.twitch.tv PRIVMSG #a :hi")
    assert isinstance(e, ChatMessage)
    assert e.is_mod


def test_usernotice_resub_with_message() -> None:
    line = (
        r"@badge-info=;badges=staff/1,broadcaster/1;color=#008000;display-name=ronni;"
        r"login=ronni;msg-id=resub;msg-param-cumulative-months=6;msg-param-streak-months=2;"
        r"msg-param-should-share-streak=1;msg-param-sub-plan=Prime;"
        r"msg-param-sub-plan-name=Prime;room-id=12345;"
        r"system-msg=ronni\shas\ssubscribed\sfor\s6\smonths!;tmi-sent-ts=1507246572675"
        r" :tmi.twitch.tv USERNOTICE #dallas :Great stream -- keep it up!"
    )
    e = ev(line)
    assert isinstance(e, UserNotice)
    assert e.channel == "dallas"
    assert e.msg_id == "resub"
    assert e.login == "ronni"
    assert e.display_name == "ronni"
    assert e.system_msg == "ronni has subscribed for 6 months!"
    assert e.text == "Great stream -- keep it up!"
    assert e.params["cumulative-months"] == "6"
    assert e.params["sub-plan"] == "Prime"
    assert "msg-param-sub-plan" not in e.params
    assert e.timestamp is not None


def test_usernotice_without_message() -> None:
    line = (
        r"@msg-id=raid;login=x;display-name=X;msg-param-viewerCount=9;"
        r"system-msg=9\sraiders\sfrom\sX\shave\sjoined! :tmi.twitch.tv USERNOTICE #y"
    )
    e = ev(line)
    assert isinstance(e, UserNotice)
    assert e.text is None
    assert e.params == {"viewerCount": "9"}
    assert e.system_msg == "9 raiders from X have joined!"


def test_join_part() -> None:
    line = ":foo!foo@foo.tmi.twitch.tv JOIN #bar"
    assert ev(line) == Join(raw=parse_line(line), channel="bar", login="foo")
    p = ev(":foo!foo@foo.tmi.twitch.tv PART #bar")
    assert isinstance(p, Part)
    assert p.login == "foo"


def test_notices() -> None:
    e = ev("@msg-id=emote_only_on :tmi.twitch.tv NOTICE #bar :This room is now in emote-only mode.")
    assert isinstance(e, Notice)
    assert e.channel == "bar"
    assert e.msg_id == "emote_only_on"
    g = ev(":tmi.twitch.tv NOTICE * :Login authentication failed")
    assert isinstance(g, Notice)
    assert g.channel is None
    assert g.msg_id is None
    assert g.text == "Login authentication failed"


def test_clearchat_and_clearmsg() -> None:
    ban = ev("@ban-duration=350;room-id=1;target-user-id=2 :tmi.twitch.tv CLEARCHAT #dallas :ronni")
    assert isinstance(ban, ClearChat)
    assert ban.login == "ronni"
    assert ban.ban_duration == 350
    clear = ev(":tmi.twitch.tv CLEARCHAT #dallas")
    assert isinstance(clear, ClearChat)
    assert clear.login is None
    assert clear.ban_duration is None
    msg = ev("@login=foo;target-msg-id=94e6 :tmi.twitch.tv CLEARMSG #bar :what a great day")
    assert isinstance(msg, ClearMsg)
    assert (msg.login, msg.target_msg_id, msg.text) == ("foo", "94e6", "what a great day")


def test_state_events() -> None:
    assert isinstance(ev("@slow=0 :tmi.twitch.tv ROOMSTATE #bar"), RoomState)
    assert isinstance(ev("@mod=0 :tmi.twitch.tv USERSTATE #bar"), UserState)
    assert isinstance(ev("@user-id=1 :tmi.twitch.tv GLOBALUSERSTATE"), GlobalUserState)


def test_misc_events() -> None:
    w = ev("@badges= :petsgomoo!petsgomoo@petsgomoo.tmi.twitch.tv WHISPER foo :hello")
    assert isinstance(w, Whisper)
    assert (w.login, w.text) == ("petsgomoo", "hello")
    assert isinstance(ev(":tmi.twitch.tv RECONNECT"), Reconnect)
    c = ev(":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!")
    assert isinstance(c, Connected)
    assert c.nick == "justinfan1"
    assert isinstance(ev(":tmi.twitch.tv 372 justinfan1 :You are in a maze"), RawEvent)
    # PRIVMSG to a non-channel target is not a ChatMessage
    assert isinstance(ev(":a!a@a PRIVMSG someone :hi"), RawEvent)


def test_parse_badges() -> None:
    assert parse_badges(None) == {}
    assert parse_badges("") == {}
    assert parse_badges("a/1,b/,c") == {"a": "1", "b": "", "c": ""}


def test_malformed_numeric_tags_are_ignored() -> None:
    line = "@bits=²;tmi-sent-ts={ts} :a!a@a PRIVMSG #c :hi"
    for ts in ("9" * 30, "²", "9" * 5000):
        msg = ev(line.format(ts=ts))
        assert isinstance(msg, ChatMessage)
        assert msg.bits is None
        assert msg.timestamp is None
    assert isinstance(e := ev("@ban-duration=" + "9" * 5000 + " :t CLEARCHAT #c :u"), ClearChat)
    assert e.ban_duration is None
