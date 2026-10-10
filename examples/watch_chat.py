"""Anonymously watch one or more Twitch channels and print parsed events.

Usage::

    uv run examples/watch_chat.py [--seconds 60] channel [channel ...]

Exits non-zero if no chat message was received within the time limit.
Read-only: anonymous (justinfan) connections cannot send messages.
"""

import argparse
import asyncio
import logging
import sys

from twitchchat import ChatMessage, Join, Notice, RoomState, TwitchChat, UserNotice


async def watch(channels: list[str], seconds: float) -> int:
    """Print events for ``seconds`` and return the number of chat messages seen."""
    count = 0
    async with TwitchChat(channels) as chat:
        print(f"connected as {chat.nick}, joining {', '.join(channels)}")

        @chat.on(UserNotice)
        def on_usernotice(event: UserNotice) -> None:
            print(f"[#{event.channel}] *** {event.msg_id}: {event.system_msg} {event.params}")

        try:
            async with asyncio.timeout(seconds):
                async for event in chat.events(ChatMessage, Join, Notice, RoomState):
                    match event:
                        case ChatMessage():
                            count += 1
                            ts = event.timestamp.strftime("%H:%M:%S") if event.timestamp else "?"
                            badges = ",".join(event.badges)
                            print(
                                f"[#{event.channel} {ts}] <{event.display_name}"
                                f"{' [' + badges + ']' if badges else ''}> {event.text!r}"
                            )
                        case Join(login=login) if login == chat.nick:
                            print(f"joined #{event.channel}")
                        case RoomState():
                            print(f"roomstate #{event.channel}: {event.tags}")
                        case Notice():
                            print(f"notice: {event.text}")
                        case _:
                            pass
        except TimeoutError:
            pass
    return count


def main() -> None:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channels", nargs="+")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    count = asyncio.run(watch(args.channels, args.seconds))
    print(f"received {count} chat messages in {args.seconds:.0f}s")
    sys.exit(0 if count else 1)


if __name__ == "__main__":
    main()
