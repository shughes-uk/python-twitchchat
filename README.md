# Synopsis

A python module aimed to wrap [twitch.tvs custom IRC implementation](https://github.com/justintv/Twitch-API/blob/master/IRC.md) and provide easy event based access to it

# Usage
'''
from twitchchat import twitch_chat

def new_message(msg):
    print msg

def new_subscriber(name,months)
    print 'New subscriber {0}! For {1} months'.format(name, months)

tirc = twitch_chat('your twitch username', 'your twitch oauth', ['geekandsundry', 'riotgames'])
tirc.subscribeChatMessage(new_message)
tirc.subscribeNewSubscriber
tirc.run()
'''

#Future
- Expose message sending somehow
