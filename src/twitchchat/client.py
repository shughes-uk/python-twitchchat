"""asyncio Twitch chat client."""

import asyncio
import contextlib
import inspect
import logging
import secrets
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Protocol, Self, overload
lazy import ssl

from .events import ChatMessage, Connected, Event, Notice, Reconnect, event_from_irc
from .irc import IrcMessage, format_line, parse_line

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AuthenticationError",
    "EventStream",
    "Listener",
    "RateLimiter",
    "TwitchChat",
    "TwitchChatError",
]

logger = logging.getLogger("twitchchat")

DEFAULT_HOST = "irc.chat.twitch.tv"
DEFAULT_PORT = 6697
DEFAULT_CAPABILITIES = ("twitch.tv/tags", "twitch.tv/commands", "twitch.tv/membership")
_AUTH_FAILURES = ("Login authentication failed", "Improperly formatted auth")

type Listener[E: Event] = Callable[[E], Awaitable[None] | None]


class TwitchChatError(Exception):
    """Base error for this library."""


class AuthenticationError(TwitchChatError):
    """Twitch rejected the nick/token."""


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` acquisitions per ``period`` seconds."""

    def __init__(self, limit: int, period: float) -> None:
        if limit < 1 or period <= 0:
            msg = "limit must be >= 1 and period > 0"
            raise ValueError(msg)
        self.limit = limit
        self.period = period
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            while True:
                now = loop.time()
                while self._stamps and now - self._stamps[0] >= self.period:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._stamps[0]))


class _Sink(Protocol):
    def _offer(self, event: Event) -> None: ...
    def _finish(self) -> None: ...


class _Closed:
    pass


_CLOSED = _Closed()


class EventStream[E: Event]:
    """Async iterator over events, created by :meth:`TwitchChat.events`.

    Buffers from the moment it is created until it is closed (``aclose()``,
    ``async with``, or the client closing).
    """

    def __init__(self, chat: TwitchChat, types: tuple[type[E], ...]) -> None:
        self._chat = chat
        self._types = types
        self._queue: asyncio.Queue[E | _Closed] = asyncio.Queue()
        self._done = False
        chat._streams.add(self)  # noqa: SLF001
        if chat.closed:
            self._finish()

    def _offer(self, event: Event) -> None:
        if not self._done and (not self._types or isinstance(event, self._types)):
            self._queue.put_nowait(event)  # ty: ignore[invalid-argument-type]

    def _finish(self) -> None:
        if not self._done:
            self._done = True
            self._queue.put_nowait(_CLOSED)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> E:
        item = await self._queue.get()
        if isinstance(item, _Closed):
            self._chat._streams.discard(self)  # noqa: SLF001
            if self._chat.exception is not None:
                raise self._chat.exception
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self._chat._streams.discard(self)  # noqa: SLF001
        self._done = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _normalize_channel(channel: str) -> str:
    return channel.strip().lstrip("#").lower()


class TwitchChat:
    """An asyncio Twitch chat (IRC) client.

    With no ``token`` it logs in anonymously (``justinfanNNNNN``), which can read
    but not send. Use as an async context manager::

        async with TwitchChat(["somechannel"]) as chat:
            async for event in chat:
                ...

    Args:
        channels: channels to join once connected (with or without ``#``).
        nick: login name of the token's account (required with ``token``).
        token: OAuth user access token with ``chat:read`` (and ``chat:edit`` to
            send); the ``oauth:`` prefix is optional.
        host, port: IRC server (TLS by default).
        tls: ``True`` for default TLS, ``False`` for plaintext, or an SSLContext.
        reconnect: reconnect automatically on disconnects / ``RECONNECT``.
        message_rate: ``(limit, period)`` for outgoing PRIVMSGs. Twitch allows 20
            per 30s for normal users, 100 per 30s where you are mod/VIP/broadcaster.
        join_rate: ``(limit, period)`` for JOINs (Twitch: 20 per 10s).
        capabilities: IRCv3 capabilities to request.
        read_timeout: seconds without any data before the connection is
            considered dead (Twitch PINGs about every 5 minutes).
    """

    def __init__(  # noqa: PLR0913
        self,
        channels: Iterable[str] = (),
        *,
        nick: str | None = None,
        token: str | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        tls: bool | ssl.SSLContext = True,
        reconnect: bool = True,
        message_rate: tuple[int, float] = (20, 30.0),
        join_rate: tuple[int, float] = (20, 10.0),
        capabilities: Iterable[str] = DEFAULT_CAPABILITIES,
        connect_timeout: float = 15.0,
        read_timeout: float = 360.0,
    ) -> None:
        if token is not None and not nick:
            msg = "nick is required when a token is given"
            raise ValueError(msg)
        self.anonymous = token is None
        self.nick = (nick or f"justinfan{secrets.randbelow(90000) + 10000}").lower()
        self._token = None if token is None else "oauth:" + token.removeprefix("oauth:")
        self.host = host
        self.port = port
        self._tls = tls
        self.reconnect = reconnect
        self.capabilities = tuple(capabilities)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._message_limiter = RateLimiter(*message_rate)
        self._join_limiter = RateLimiter(*join_rate)

        self.channels: set[str] = {_normalize_channel(c) for c in channels}
        self._listeners: dict[type[Event], list[Listener[Event]]] = {}
        self._streams: set[_Sink] = set()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ready = asyncio.Event()
        self._welcome: asyncio.Future[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._reconnect_requested = False
        self._background: set[asyncio.Task[None]] = set()
        self._started = False
        self.exception: BaseException | None = None

    # -- lifecycle -----------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    async def connect(self) -> None:
        """Connect, authenticate and join the configured channels.

        Raises:
            AuthenticationError: if Twitch rejects the credentials.
            TimeoutError: if the server does not welcome us in time.
            OSError: on network errors.
        """
        if self._started:
            msg = "connect() may only be called once"
            raise TwitchChatError(msg)
        self._started = True
        try:
            await self._open()
        except BaseException:
            await self._shutdown(None)
            raise
        self._task = asyncio.create_task(self._run(), name=f"twitchchat-{self.nick}")

    async def close(self) -> None:
        """Close the connection and end all event streams."""
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._shutdown(None)

    async def wait_closed(self) -> None:
        """Wait until the client closes; re-raise the error that closed it, if any."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self._task)
        if self.exception is not None:
            raise self.exception

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- listeners & streams ---------------------------------------------------------

    def add_listener[E: Event](self, event_type: type[E], callback: Listener[E]) -> None:
        """Call ``callback(event)`` for every event of ``event_type`` (or a subclass).

        Callbacks may be plain functions or coroutine functions; they run in order
        on the reader task, so long-running work should be offloaded.
        """
        self._listeners.setdefault(event_type, []).append(callback)  # ty: ignore[invalid-argument-type]

    def remove_listener[E: Event](self, event_type: type[E], callback: Listener[E]) -> None:
        with contextlib.suppress(KeyError, ValueError):
            self._listeners[event_type].remove(callback)  # ty: ignore[invalid-argument-type]

    def on[E: Event](self, event_type: type[E]) -> Callable[[Listener[E]], Listener[E]]:
        """Decorator form of :meth:`add_listener`."""

        def decorator(callback: Listener[E]) -> Listener[E]:
            self.add_listener(event_type, callback)
            return callback

        return decorator

    @overload
    def events(self) -> EventStream[Event]: ...
    @overload
    def events[E: Event](self, *types: type[E]) -> EventStream[E]: ...
    def events(self, *types: type[Event]) -> EventStream[Event]:
        """Return an async iterator of events, optionally filtered by type."""
        return EventStream(self, types)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    # -- commands --------------------------------------------------------------------

    async def send(self, channel: str, text: str, *, reply_to: str | None = None) -> None:
        """Send a chat message (rate limited). ``reply_to`` is a message id to reply to.

        Raises:
            TwitchChatError: when anonymous or closed.
        """
        if self.anonymous:
            msg = "anonymous connections cannot send messages; pass nick and token"
            raise TwitchChatError(msg)
        tags = {"reply-parent-msg-id": reply_to} if reply_to else None
        line = format_line("PRIVMSG", "#" + _normalize_channel(channel), text, tags=tags)
        await self._message_limiter.acquire()
        await self._send_line(line)

    async def reply(self, message: ChatMessage, text: str) -> None:
        """Reply to a chat message in its channel (threaded reply)."""
        await self.send(message.channel, text, reply_to=message.id)

    async def join(self, channel: str) -> None:
        """Join a channel (and rejoin it after reconnects)."""
        name = _normalize_channel(channel)
        self.channels.add(name)
        if self.connected:
            await self._join_limiter.acquire()
            await self._send_line(format_line("JOIN", "#" + name))

    async def part(self, channel: str) -> None:
        """Leave a channel."""
        name = _normalize_channel(channel)
        self.channels.discard(name)
        if self.connected:
            await self._send_line(format_line("PART", "#" + name))

    async def send_raw(self, line: str) -> None:
        """Send a raw IRC line (CRLF is appended if missing). Not rate limited."""
        await self._send_line(line if line.endswith("\r\n") else line + "\r\n")

    # -- internals -------------------------------------------------------------------

    async def _send_line(self, line: str) -> None:
        if self._closed:
            msg = "client is closed"
            raise TwitchChatError(msg)
        if not self._ready.is_set():
            waiters = [
                asyncio.ensure_future(self._ready.wait()),
                asyncio.ensure_future(self._closed_event.wait()),
            ]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
        writer = self._writer
        if writer is None:
            msg = "not connected"
            raise TwitchChatError(msg)
        logger.debug("> %s", line.rstrip())
        writer.write(line.encode())
        await writer.drain()

    def _write_now(self, line: str) -> None:
        if self._writer is not None:
            if not line.startswith("PASS"):
                logger.debug("> %s", line.rstrip())
            self._writer.write(line.encode())

    async def _open(self) -> None:
        ssl_arg: ssl.SSLContext | bool | None = self._tls or None
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ssl_arg),
            self.connect_timeout,
        )
        self._welcome = asyncio.get_running_loop().create_future()
        if self.capabilities:
            self._write_now(format_line("CAP", "REQ", " ".join(self.capabilities)))
        self._write_now(format_line("PASS", self._token or "SCHMOOPIIE"))
        self._write_now(format_line("NICK", self.nick))
        await self._writer.drain()
        # Read until welcomed (or rejected) without the main loop running yet.
        async with asyncio.timeout(self.connect_timeout):
            while not self._welcome.done():
                if not await self._read_one():
                    msg = "connection closed during login"
                    raise ConnectionError(msg)
        self._welcome.result()

    async def _read_one(self) -> bool:
        """Read and handle one line. Returns False on EOF."""
        assert self._reader is not None  # noqa: S101
        data = await self._reader.readline()
        if not data:
            return False
        line = data.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            return True
        logger.debug("< %s", line)
        try:
            msg = parse_line(line)
        except ValueError:
            logger.warning("unparseable line: %r", line)
            return True
        await self._handle(msg)
        return True

    async def _handle(self, msg: IrcMessage) -> None:
        if msg.command == "PING":
            self._write_now(format_line("PONG", msg.trailing or "tmi.twitch.tv"))
            return
        event = event_from_irc(msg)
        if isinstance(event, Connected):
            self._on_welcome()
        elif isinstance(event, Reconnect):
            self._reconnect_requested = True
        elif (
            isinstance(event, Notice)
            and event.text.startswith(_AUTH_FAILURES)
            and self._welcome is not None
            and not self._welcome.done()
        ):
            self._welcome.set_exception(AuthenticationError(event.text))
        await self._dispatch(event)

    def _on_welcome(self) -> None:
        self._ready.set()
        if self._welcome is not None and not self._welcome.done():
            self._welcome.set_result(None)
        # Each channel counts as a join attempt; join in the background, rate limited.
        if self.channels:
            task = asyncio.create_task(self._join_all(sorted(self.channels)))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _join_all(self, channels: list[str]) -> None:
        for name in channels:
            await self._join_limiter.acquire()
            if name in self.channels and self._ready.is_set():
                with contextlib.suppress(OSError, TwitchChatError):
                    await self._send_line(format_line("JOIN", "#" + name))

    async def _dispatch(self, event: Event) -> None:
        for stream in list(self._streams):
            stream._offer(event)  # noqa: SLF001
        for event_type, callbacks in list(self._listeners.items()):
            if not isinstance(event, event_type):
                continue
            for callback in list(callbacks):
                try:
                    result = callback(event)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("error in listener %r", callback)

    async def _run(self) -> None:
        error: BaseException | None = None
        backoff = 1.0
        try:
            while True:
                reason = await self._read_loop()
                self._drop_connection()
                if not self.reconnect:
                    break
                logger.info("disconnected (%s); reconnecting", reason)
                while True:
                    try:
                        await self._open()
                        backoff = 1.0
                        break
                    except AuthenticationError:
                        raise
                    except (OSError, TimeoutError, ConnectionError) as e:
                        logger.warning("reconnect failed: %s; retrying in %.0fs", e, backoff)
                        self._drop_connection()
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            error = e
        finally:
            await self._shutdown(error)

    async def _read_loop(self) -> str:
        while True:
            try:
                async with asyncio.timeout(self.read_timeout):
                    alive = await self._read_one()
            except TimeoutError:
                return "read timeout"
            except OSError as e:
                return f"error: {e}"
            if not alive:
                return "connection closed by server"
            if self._reconnect_requested:
                self._reconnect_requested = False
                return "server requested reconnect"

    def _drop_connection(self) -> None:
        self._ready.clear()
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None

    async def _shutdown(self, error: BaseException | None) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self.exception = error
        for task in list(self._background):
            task.cancel()
        writer = self._writer
        self._drop_connection()
        if writer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
        for stream in list(self._streams):
            stream._finish()  # noqa: SLF001
