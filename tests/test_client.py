"""Client tests against a small fake Twitch IRC server on localhost."""

import asyncio
import gc
import logging
from typing import TYPE_CHECKING

import pytest

from twitchchat import (
    AuthenticationError,
    ChatMessage,
    Connected,
    Event,
    Join,
    RateLimiter,
    RoomState,
    TwitchChat,
    TwitchChatError,
    UserNotice,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PRIVMSG = (
    "@badges=subscriber/1;display-name=Foo;id=m1;tmi-sent-ts=1700000000000"
    " :foo!foo@foo.tmi.twitch.tv PRIVMSG #{chan} :{text}"
)


class FakeTwitch:
    def __init__(self) -> None:
        self.received: list[str] = []
        self.connections = 0
        self.writer: asyncio.StreamWriter | None = None
        self.line_event = asyncio.Condition()
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.writer is not None:
            self.writer.close()
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writer = writer
        nick = ""
        password = ""
        try:
            while line := (await reader.readline()).decode():
                line = line.rstrip("\r\n")
                async with self.line_event:
                    self.received.append(line)
                    self.line_event.notify_all()
                cmd, _, rest = line.partition(" ")
                if cmd == "CAP":
                    self.send(f":tmi.twitch.tv CAP * ACK :{rest.split(':', 1)[1]}")
                elif cmd == "PASS":
                    password = rest
                elif cmd == "NICK":
                    nick = rest
                    if password == "oauth:bad":
                        self.send(":tmi.twitch.tv NOTICE * :Login authentication failed")
                        await writer.drain()
                        break
                    self.send(f":tmi.twitch.tv 001 {nick} :Welcome, GLHF!")
                    self.send(f":tmi.twitch.tv 376 {nick} :>")
                elif cmd == "JOIN":
                    chan = rest
                    self.send(f":{nick}!{nick}@{nick}.tmi.twitch.tv JOIN {chan}")
                    self.send(f"@room-id=1;slow=0 :tmi.twitch.tv ROOMSTATE {chan}")
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write((line + "\r\n").encode())

    async def push(self, line: str) -> None:
        self.send(line)
        assert self.writer is not None
        await self.writer.drain()

    async def wait_for_line(self, prefix: str, count: int = 1) -> list[str]:
        async with asyncio.timeout(5), self.line_event:
            await self.line_event.wait_for(
                lambda: len([x for x in self.received if x.startswith(prefix)]) >= count
            )
        return [x for x in self.received if x.startswith(prefix)]


@pytest.fixture
async def fake() -> AsyncIterator[FakeTwitch]:
    server = FakeTwitch()
    await server.start()
    yield server
    await server.stop()


def make_chat(fake: FakeTwitch, channels: list[str] | None = None, **kwargs: object) -> TwitchChat:
    return TwitchChat(
        channels or [],
        host="127.0.0.1",
        port=fake.port,
        tls=False,
        **kwargs,  # ty: ignore[invalid-argument-type]
    )


async def next_of[E: Event](stream: AsyncIterator[Event], kind: type[E]) -> E:
    async with asyncio.timeout(5):
        async for event in stream:
            if isinstance(event, kind):
                return event
    msg = "stream ended"
    raise AssertionError(msg)


async def test_anonymous_handshake_and_join(fake: FakeTwitch) -> None:
    async with make_chat(fake, ["#SomeChannel", "other"]) as chat:
        assert chat.anonymous
        assert chat.nick.startswith("justinfan")
        assert chat.connected
        joins = await fake.wait_for_line("JOIN", 2)
    assert fake.received[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership"
    assert fake.received[1] == "PASS SCHMOOPIIE"
    assert fake.received[2] == f"NICK {chat.nick}"
    assert sorted(joins) == ["JOIN #other", "JOIN #somechannel"]
    assert chat.closed


async def test_authenticated_handshake(fake: FakeTwitch) -> None:
    async with make_chat(fake, nick="MyBot", token="abc123") as chat:
        assert not chat.anonymous
        assert chat.nick == "mybot"
    assert "PASS oauth:abc123" in fake.received
    assert "NICK mybot" in fake.received


def test_token_requires_nick() -> None:
    with pytest.raises(ValueError, match="nick"):
        TwitchChat(token="abc")


async def test_auth_failure(fake: FakeTwitch) -> None:
    chat = make_chat(fake, nick="me", token="oauth:bad")
    with pytest.raises(AuthenticationError, match="Login authentication failed"):
        await chat.connect()
    assert chat.closed


async def test_connect_refused() -> None:
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    chat = TwitchChat(host="127.0.0.1", port=port, tls=False)
    with pytest.raises(OSError):  # noqa: PT011
        await chat.connect()


async def test_ping_pong(fake: FakeTwitch) -> None:
    async with make_chat(fake):
        await fake.push("PING :tmi.twitch.tv")
        assert await fake.wait_for_line("PONG") == ["PONG tmi.twitch.tv"]


async def test_event_stream_and_filters(fake: FakeTwitch) -> None:
    async with make_chat(fake, ["chan"]) as chat:
        all_events = chat.events()
        chat_only = chat.events(ChatMessage)
        await fake.wait_for_line("JOIN")
        await fake.push(PRIVMSG.format(chan="chan", text="hello world"))
        await fake.push(r"@msg-id=sub;system-msg=a\ssub :tmi.twitch.tv USERNOTICE #chan :yay")

        room = await next_of(all_events, RoomState)
        assert room.channel == "chan"
        msg = await next_of(chat_only, ChatMessage)
        assert (msg.channel, msg.login, msg.display_name, msg.text) == (
            "chan",
            "foo",
            "Foo",
            "hello world",
        )
        assert msg.badges == {"subscriber": "1"}
        notice = await next_of(all_events, UserNotice)
        assert notice.system_msg == "a sub"
        assert notice.text == "yay"
        await all_events.aclose()
        await chat_only.aclose()


async def test_iterator_ends_on_close(fake: FakeTwitch) -> None:
    chat = make_chat(fake)
    await chat.connect()
    seen: list[Event] = []

    async def consume() -> None:
        seen.extend([e async for e in chat])

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await chat.close()
    async with asyncio.timeout(5):
        await task
    # a stream created after close ends immediately
    assert [e async for e in chat.events()] == []


async def test_callbacks_sync_async_and_errors(
    fake: FakeTwitch, caplog: pytest.LogCaptureFixture
) -> None:
    chat = make_chat(fake, ["chan"])
    seen: list[str] = []
    connected: list[Connected] = []
    done = asyncio.Event()

    chat.add_listener(Connected, connected.append)

    @chat.on(ChatMessage)
    def sync_cb(event: ChatMessage) -> None:
        seen.append("sync:" + event.text)

    @chat.on(ChatMessage)
    def broken(_event: ChatMessage) -> None:
        raise RuntimeError("boom")

    @chat.on(Event)
    async def async_cb(event: Event) -> None:
        if isinstance(event, ChatMessage):
            seen.append("async:" + event.text)
            done.set()

    async with chat:
        await fake.wait_for_line("JOIN")
        with caplog.at_level(logging.ERROR, logger="twitchchat"):
            await fake.push(PRIVMSG.format(chan="chan", text="hi"))
            async with asyncio.timeout(5):
                await done.wait()
        chat.remove_listener(ChatMessage, sync_cb)
        chat.remove_listener(ChatMessage, sync_cb)  # removing twice is harmless
    assert seen == ["sync:hi", "async:hi"]
    assert len(connected) == 1
    assert "error in listener" in caplog.text


async def test_send_and_reply(fake: FakeTwitch) -> None:
    async with make_chat(fake, nick="bot", token="tok") as chat:
        await chat.send("#Chan", "hello there")
        await chat.send("chan", ":)")
        msg_stream = chat.events(ChatMessage)
        await fake.push(PRIVMSG.format(chan="chan", text="question"))
        msg = await next_of(msg_stream, ChatMessage)
        await chat.reply(msg, "answer")
        lines = await fake.wait_for_line("@reply-parent-msg-id")
        await chat.send_raw("PRIVMSG #chan :raw")
        await fake.wait_for_line("PRIVMSG #chan :raw")
    assert "PRIVMSG #chan :hello there" in fake.received
    assert "PRIVMSG #chan ::)" in fake.received
    assert lines == ["@reply-parent-msg-id=m1 PRIVMSG #chan answer"]


async def test_send_rejects_injection(fake: FakeTwitch) -> None:
    async with make_chat(fake, nick="bot", token="tok") as chat:
        with pytest.raises(ValueError, match="CR or LF"):
            await chat.send("chan", "hi\r\nJOIN #evil")


async def test_anonymous_cannot_send(fake: FakeTwitch) -> None:
    async with make_chat(fake) as chat:
        with pytest.raises(TwitchChatError, match="anonymous"):
            await chat.send("chan", "hi")


async def test_send_after_close_raises(fake: FakeTwitch) -> None:
    chat = make_chat(fake, nick="bot", token="tok")
    async with chat:
        pass
    with pytest.raises(TwitchChatError, match="closed"):
        await chat.send("chan", "hi")


async def test_join_and_part_at_runtime(fake: FakeTwitch) -> None:
    async with make_chat(fake) as chat:
        joins = chat.events(Join)
        await chat.join("#NewChan")
        join = await next_of(joins, Join)
        assert (join.channel, join.login) == ("newchan", chat.nick)
        assert chat.channels == {"newchan"}
        await chat.part("newchan")
        await fake.wait_for_line("PART #newchan")
        assert chat.channels == set()


async def test_reconnect_command_rejoins(fake: FakeTwitch) -> None:
    async with make_chat(fake, ["chan"]) as chat:
        connected = chat.events(Connected)
        await fake.wait_for_line("JOIN #chan")
        await fake.push(":tmi.twitch.tv RECONNECT")
        await next_of(connected, Connected)
        await fake.wait_for_line("JOIN #chan", 2)
        assert fake.connections == 2
        assert chat.connected


async def test_server_drop_reconnects(fake: FakeTwitch) -> None:
    async with make_chat(fake, ["chan"]) as chat:
        connected = chat.events(Connected)
        await fake.wait_for_line("JOIN #chan")
        assert fake.writer is not None
        fake.writer.close()
        await next_of(connected, Connected)
        await fake.wait_for_line("JOIN #chan", 2)
        assert fake.connections == 2


async def test_server_drop_without_reconnect_closes(fake: FakeTwitch) -> None:
    chat = make_chat(fake, reconnect=False)
    await chat.connect()
    stream = chat.events()
    assert fake.writer is not None
    fake.writer.close()
    async with asyncio.timeout(5):
        await chat.wait_closed()
        _ = [e async for e in stream]
    assert chat.closed
    assert chat.exception is None


async def test_connect_twice_rejected(fake: FakeTwitch) -> None:
    async with make_chat(fake) as chat:
        with pytest.raises(TwitchChatError, match="once"):
            await chat.connect()


async def test_rate_limiter() -> None:
    limiter = RateLimiter(2, 0.2)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(5):
        await limiter.acquire()
    elapsed = loop.time() - start
    # 5 acquisitions with 2 per 0.2s need two full waits
    assert 0.35 <= elapsed < 2


def test_rate_limiter_validation() -> None:
    with pytest.raises(ValueError, match="limit"):
        RateLimiter(0, 1)


async def test_malformed_lines_do_not_kill_reader(fake: FakeTwitch) -> None:
    async with make_chat(fake) as chat:
        messages = chat.events(ChatMessage)
        await fake.push("PING :a\rb")  # PONG would contain CR -> format_line ValueError
        await fake.push(":only.a.prefix")
        await fake.push(PRIVMSG.format(chan="chan", text="still alive"))
        msg = await next_of(messages, ChatMessage)
        assert msg.text == "still alive"
        assert chat.connected
        assert not chat.closed


async def test_send_raw_rejects_embedded_newlines(fake: FakeTwitch) -> None:
    async with make_chat(fake, nick="bot", token="tok") as chat:
        with pytest.raises(ValueError, match="single IRC line"):
            await chat.send_raw("PRIVMSG #chan :hi\r\nJOIN #evil")
        await chat.send_raw("PRIVMSG #chan :ok\r\n")
        await fake.wait_for_line("PRIVMSG #chan :ok")


@pytest.mark.parametrize("bad", ["", "#", "a b", "a,b", "a\r\nQUIT"])
async def test_invalid_channel_names_rejected(fake: FakeTwitch, bad: str) -> None:
    with pytest.raises(ValueError, match="invalid channel"):
        TwitchChat([bad])
    async with make_chat(fake, nick="bot", token="tok") as chat:
        with pytest.raises(ValueError, match="invalid channel"):
            await chat.join(bad)
        with pytest.raises(ValueError, match="invalid channel"):
            await chat.send(bad, "hi")


async def test_stream_ends_for_every_consumer_and_after_close(fake: FakeTwitch) -> None:
    chat = make_chat(fake)
    await chat.connect()
    stream = chat.events()

    async def drain() -> None:
        async for _ in stream:
            pass

    tasks = [asyncio.create_task(drain()) for _ in range(3)]
    await asyncio.sleep(0)
    await chat.close()
    async with asyncio.timeout(5):
        await asyncio.gather(*tasks)
        assert [e async for e in stream] == []  # iterating again also ends


async def test_aclose_wakes_waiting_consumer(fake: FakeTwitch) -> None:
    async with make_chat(fake) as chat:
        stream = chat.events(ChatMessage)
        task = asyncio.create_task(anext(stream, None))
        await asyncio.sleep(0)
        await stream.aclose()
        async with asyncio.timeout(5):
            assert await task is None


async def test_abandoned_stream_is_not_retained(fake: FakeTwitch) -> None:
    async with make_chat(fake, ["chan"]) as chat:
        async for _ in chat:
            break  # never aclose()d
        gc.collect()
        assert len(chat._streams) == 0


async def test_stream_maxsize_drops_oldest(
    fake: FakeTwitch, caplog: pytest.LogCaptureFixture
) -> None:
    async with make_chat(fake, ["chan"]) as chat:
        stream = chat.events(ChatMessage, maxsize=2)
        last = chat.events(ChatMessage)
        with caplog.at_level(logging.WARNING, logger="twitchchat"):
            for i in range(5):
                await fake.push(PRIVMSG.format(chan="chan", text=str(i)))
            for _ in range(5):
                await next_of(last, ChatMessage)
        assert stream.dropped == 3
        assert [(await anext(stream)).text for _ in range(2)] == ["3", "4"]
        assert "dropping oldest" in caplog.text


async def test_close_from_listener(fake: FakeTwitch) -> None:
    chat = make_chat(fake, ["chan"])

    @chat.on(ChatMessage)
    async def stop(_event: ChatMessage) -> None:
        await chat.close()

    await chat.connect()
    await fake.wait_for_line("JOIN")
    await fake.push(PRIVMSG.format(chan="chan", text="bye"))
    async with asyncio.timeout(5):
        await chat.wait_closed()
    assert chat.closed
    assert chat.exception is None
    assert fake.connections == 1


async def test_fast_disconnects_back_off(fake: FakeTwitch) -> None:
    async with make_chat(fake) as chat:
        assert fake.writer is not None
        fake.writer.close()
        await asyncio.sleep(0.5)
        # dropped right after connecting: wait (1s backoff) before reconnecting
        assert fake.connections == 1
        connected = chat.events(Connected)
        await next_of(connected, Connected)
        assert fake.connections == 2
