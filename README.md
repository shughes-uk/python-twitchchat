# python-twitchchat

An asyncio client for [Twitch chat over IRC](https://dev.twitch.tv/docs/chat/irc/).
It has no runtime dependencies and gives you typed events, an async iterator,
callback registration, rate-limited sending, automatic PING/PONG and automatic
reconnects.

- **Requirements:** Python 3.15 or newer, standard library only.
- **Install:** `pip install python-twitchchat` or `uv add python-twitchchat`. The import name is `twitchchat`.
- Connects to `irc.chat.twitch.tv:6697` over TLS.

## Usage

### Reading chat anonymously

If you don't pass a token, the client logs in as `justinfanNNNNN`. That's
enough to read chat, but not to send.

```python
import asyncio

from twitchchat import ChatMessage, TwitchChat, UserNotice


async def main() -> None:
    async with TwitchChat(["somechannel", "#another"]) as chat:
        async for event in chat:  # every event; or chat.events(ChatMessage, UserNotice)
            match event:
                case ChatMessage():
                    print(f"[#{event.channel}] <{event.display_name}> {event.text}")
                case UserNotice():  # sub, resub, subgift, raid, announcement, ...
                    print(f"[#{event.channel}] {event.msg_id}: {event.system_msg}")


asyncio.run(main())
```

### Callbacks and sending messages

```python
import asyncio

from twitchchat import ChatMessage, TwitchChat, UserNotice


async def main() -> None:
    chat = TwitchChat(["mychannel"], nick="mybot", token="oauth-token-with-chat:read-chat:edit")

    @chat.on(ChatMessage)
    async def on_message(msg: ChatMessage) -> None:
        if msg.text == "!ping":
            await chat.reply(msg, "pong")  # threaded reply

    @chat.on(UserNotice)
    def on_sub(notice: UserNotice) -> None:  # plain functions work too
        if notice.msg_id in {"sub", "resub"}:
            print(notice.login, notice.params.get("cumulative-months"))

    async with chat:
        await chat.send("mychannel", "hello chat")
        await chat.wait_closed()  # run until closed or a fatal error


asyncio.run(main())
```

### API overview

`TwitchChat(channels, *, nick=None, token=None, reconnect=True, message_rate=(20, 30.0), join_rate=(20, 10.0), ...)`

| Member | Purpose |
| --- | --- |
| `async with chat` / `await chat.connect()` / `await chat.close()` | Connect and authenticate, then join the configured channels. A bad token raises `AuthenticationError`. |
| `async for event in chat` / `chat.events(*types)` | Async iterator of events, optionally filtered by type. It buffers from the moment it is created and ends when the client closes. |
| `chat.on(EventType)` / `add_listener` / `remove_listener` | Register sync or async callbacks. They also match subclasses, so `chat.on(Event)` receives everything. |
| `await chat.send(channel, text, reply_to=None)` / `await chat.reply(msg, text)` | Send a message, rate limited client-side. Anonymous sessions raise `TwitchChatError`. |
| `await chat.join(channel)` / `await chat.part(channel)` | Change channels at runtime. Joined channels are rejoined after a reconnect. |
| `await chat.send_raw(line)` | Send a raw IRC line. |
| `await chat.wait_closed()` | Wait for the client to close, re-raising any fatal error. |

**Events** are frozen dataclasses. Each one has `.raw` (the parsed `IrcMessage`) and `.tags`:

| Event | IRC message |
| --- | --- |
| `ChatMessage` | `PRIVMSG` |
| `UserNotice` | `USERNOTICE`: subs, raids, announcements and similar |
| `Join`, `Part` | `JOIN`, `PART` |
| `Notice` | `NOTICE` |
| `ClearChat`, `ClearMsg` | `CLEARCHAT`, `CLEARMSG` |
| `RoomState`, `UserState`, `GlobalUserState` | `ROOMSTATE`, `USERSTATE`, `GLOBALUSERSTATE` |
| `Whisper` | `WHISPER` |
| `Reconnect` | `RECONNECT` (the client reconnects on its own) |
| `Connected` | `001`, emitted on every (re)connect |
| `RawEvent` | Anything else |

The low-level parser is public: `twitchchat.parse_line()` and `format_line()`
handle IRCv3 tags, including escaped values such as `\s`, `\:` and `\\`.

### Migrating from 0.1

The old thread-based `twitch_chat(user, oauth, channels, client_id)` class has
been replaced:

| Old | New |
| --- | --- |
| `subscribeChatMessage(cb)` | `chat.on(ChatMessage)(cb)` |
| `subscribeUsernotice(cb)` | `chat.on(UserNotice)(cb)` |
| `send_message(channel, msg)` | `await chat.send(channel, msg)` |
| `start()` / `join()` / `stop()` | `async with` / `wait_closed()` / `close()` |

Callbacks now get typed dataclasses instead of dicts of raw tags; raw tags are
still available as `event.tags`. The unused `client_id` argument has been
dropped, because the library makes no HTTP API calls.

## Twitch platform notes (checked October 2026)

- **IRC still works, but Twitch recommends EventSub.** The product lifecycle
  page lists Chat (IRC) as active, and no shutdown date has been announced.
  Twitch's [IRC migration guide](https://dev.twitch.tv/docs/chat/irc-migration/)
  recommends that new bots read chat through EventSub (`channel.chat.message`)
  and send through the Helix *Send Chat Message* API. Some newer features only
  exist there.
- **Endpoints:** use `irc.chat.twitch.tv:6697` (TLS) or `wss://irc-ws.chat.twitch.tv:443`.
  Non-TLS WebSocket connections were decommissioned on 2025-08-15. The old code
  used plaintext port 6667; this version uses TLS by default.
- **Auth:** `PASS oauth:<user access token>` followed by `NICK <login>`. Reading
  needs the `chat:read` scope and sending needs `chat:edit`. You can get a token
  through any OAuth flow, for example the
  [device code flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow).
  Anonymous `justinfan` logins aren't documented, but they still work for
  reading; this was verified live.
- **Rate limits:** normal accounts get 20 messages per 30 seconds, plus 1 message
  per second per channel. Where you are mod, VIP or broadcaster the limit is 100
  per 30 seconds, so pass `message_rate=(100, 30.0)`. Joins are limited to 20 per
  10 seconds. Going over the message limit can get your messages ignored for an
  hour. The client limits itself to the normal-user defaults.
- **Kraken (v5) is gone:** it was shut down in February 2023. This library never
  needed it, and the old `client_id` parameter did nothing, so it was removed.
  Channel hosting (`HOSTTARGET`) was also removed by Twitch.

## Example

[`examples/watch_chat.py`](examples/watch_chat.py) watches channels anonymously
and prints parsed events. It exits non-zero if no chat message arrives within
the time limit.

```sh
uv run examples/watch_chat.py --seconds 60 somebigchannel anotherone
```

## Development

The project uses [uv](https://docs.astral.sh/uv/). `.python-version` pins
Python 3.15, which uv downloads automatically.

```sh
uv sync                  # create .venv with dev tools (ruff, ty, pytest, pytest-asyncio)
uv run ruff check        # lint
uv run ruff format       # format (use --check in CI)
uv run ty check          # type check
uv run pytest            # unit tests (parser + client against a local fake IRC server)
```

CI (`.github/workflows/ci.yml`) runs all four checks on every push and pull
request. Tests run on Linux, Windows and macOS.

## Releasing

The version comes from the git tag through `hatch-vcs`, so there is no version
number to edit by hand.

1. One-time setup on PyPI: go to *Your projects → Publishing → Add a new pending
   publisher* (or the project's *Settings → Publishing* once it exists). Choose
   GitHub and fill in:
   - owner `shughes-uk`
   - repository `python-twitchchat`
   - workflow `release.yml`
   - environment `pypi`

   Then create an environment named `pypi` in the GitHub repository settings.
   You can add required reviewers to it if you want manual approval before each
   publish.
2. Tag and push:
   ```sh
   git tag v2.0.0
   git push origin v2.0.0
   ```
3. `.github/workflows/release.yml` then:
   - runs CI
   - runs `uv build` and checks the built version matches the tag
   - publishes to PyPI with trusted publishing (no API token needed)
   - creates a GitHub Release with generated notes and the sdist and wheel attached

   Tags such as `v2.1.0rc1` are marked as pre-releases.

Note that the distribution name is **`python-twitchchat`**, because `twitchchat`
is already taken on PyPI by an unrelated project.
