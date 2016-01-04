# Synopsis

A python module aimed to wrap [twitch.tvs custom IRC implementation](https://github.com/justintv/Twitch-API/blob/master/IRC.md) and provide easy event based access to it

# Usage
```python
from twitchchat import twitch_chat
import logging


def new_message(msg):
    print(msg)


def new_subscriber(name, months):
    print('New subscriber {0}! For {1} months'.format(name, months))

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)
tirc = twitch_chat('animaggus', 'yourtwitchoauth', ['geekandsundry', 'riotgames'])
tirc.subscribeChatMessage(new_message)
tirc.subscribeNewSubscriber(new_subscriber)
tirc.start()
tirc.join()
```

#Future
- Expose message sending somehow
