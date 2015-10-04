import socket
import re
import logging
import time
import traceback
from threading import Thread
import asynchat, asyncore
import urllib
import json
logger = logging.getLogger(name="tmi")

class twitch_chat(object):
    def __init__(self,user, oauth,channels):
        channel_servers = {}
        for channel in channels:
            url = "http://api.twitch.tv/api/channels/{0}/chat_properties".format(channel)
            response = urllib.urlopen(url)
            data = json.loads(response.read())
            for server in data["chat_servers"]:
                if server in channel_servers:
                    channel_servers[server].add(channel)
                else:
                    channel_servers[server] = set()
                    channel_servers[server].add(channel)
        self.required_servers = self.eliminate_duplicate_servers(channel_servers)
        self.irc_handlers = []
        for server in self.required_servers:
            handler = twitchirc_handler("192.16.64.182:6667",user,oauth,self.required_servers[server])
            handler.subscribeChatMessage(self.handle_message)
            handler.subscribeNewSubscriber(self.handle_sub)
            self.irc_handlers.append(handler)
        print self.irc_handlers
        try:
            for handler in self.irc_handlers:
                handler.start()
            while True:
                time.sleep(0.1)
        finally:
            for handler in self.irc_handlers:
                handler.stop()
        # try:
        #     alerter.start()
        #     while alerter.running:
        #         time.sleep(0.1)
        # finally:
        #     logger.info("Stopping")
        #     alerter.stop()
    def handle_message(result):
        logger.info(u"{0}|{1} : {2}".format(result["channel"], result["username"],
                                            repr(result["message"])))


    def handle_sub(name, months):
        logger.info(u"{0} subbed {1} months".format(name, months))

    def eliminate_duplicate_servers(self,channel_servers):
        for key in channel_servers.keys():
            if key in channel_servers:
                for other_key in [k for k in channel_servers.keys() if not k == key]:
                    if other_key in channel_servers:
                        if channel_servers[other_key].issubset(channel_servers[key]):
                            del channel_servers[other_key]
        for server in channel_servers:
            for channel in channel_servers[server]:
                for other_server in [k for k in channel_servers.keys() if not k == server]:
                    if channel in channel_servers[other_server]:
                         channel_servers[other_server].remove(channel)
        return channel_servers

        #     self.channel_servers.append((channel,data["chat_servers"],[]))
        # for i in range(len(self.channel_servers)):
        #     for channel,servers in [c for c in self.channel_servers if not c == self.channel_servers[i]]:
        #         for server in servers:
        #             if server in self.channel_servers[i][1]:
        #                 if channel not in self.channel_servers[i][2]:
        #                     self.channel_servers[i][1].append(channel)
        # print self.channel_servers

class twitchirc_handler(asynchat.async_chat, object):
    def __init__(self, server, user, oauth, channels):
        self.logger = logging.getLogger(name="tmi[{0}]".format(server))
        self.logger.info('TMI initializing')
        self.map = {}
        asynchat.async_chat.__init__(self,map=self.map)
        self.received_data = ""
        self.user = user
        self.oauth = oauth
        self.set_terminator('\n')
        self.ircChans = channels
        self.chat_subscribers = []
        self.sub_subscribers = []
        self.asynloop_thread = Thread(target=self.run)
        self.running = False
        self.logger.info('TMI initialized')
        return

    def handle_connect(self):
        "Socked connected successfully, handle authentication and channel joining"
        self.logger.info('Connected..authenticating as %s' % self.user)
        self.push(str('Pass ' + self.oauth + '\r\n').encode('UTF-8'))
        self.push(str('NICK ' + self.user + '\r\n').lower().encode('UTF-8'))
        self.push(str('CAP REQ :twitch.tv/tags\r\n').encode('UTF-8'))
        self.logger.info('Joining channels %s' % self.ircChans)
        for chan in self.ircChans:
            self.push(str('JOIN ' + chan + '\r\n').encode('UTF-8'))

    def handle_error(self):
        #traceback.print_exc(sys.stderr)
        if self.socket:
            self.close()
        raise

    def collect_incoming_data(self, data):
        print data
        "Dump recieved data into a buffer"
        self.received_data += (data)

    def found_terminator(self):
        "Processes each line of text received from the IRC server."
        txt = self.received_data.rstrip('\r')  #accept RFC-compliant and non-RFC-compliant lines.
        self.received_data = ""
        self.handleIRCMessage(txt)  #this is the part where you put more stuff.  You may want something to respond to
        #server pings, for example

    def subscribeChatMessage(self, callback):
        "Subscribe to a callback for incoming chat messages"
        self.chat_subscribers.append(callback)

    def subscribeNewSubscriber(self, callback):
        "Subscribe to a callback for new subscribers and resubs"
        self.sub_subscribers.append(callback)

    def check_error(self, ircMessage):
        "Check for a login error notification and terminate if found"
        if re.search(r":tmi.twitch.tv NOTICE \* :Error logging i.*",
                     ircMessage):
            self.logger.critical(
                "Error logging in to twitch irc, check your oauth and username are set correctly in config.txt!")
            self.stop()

    def check_join(self, ircMessage):
        "Watch for successful channel join messages"
        for chan in self.ircChans:
            if ircMessage.find("JOIN %s" % chan) != -1:
                self.logger.info(
                    "Joined channel %s successfully... watching for new subscribers"
                    % chan)
                return True

    def check_subscriber(self, ircMessage):
        "Parse out new twitch subscriber messages and then call... python subscribers"
        match = re.search(
            r":twitchnotify!twitchnotify@twitchnotify\.tmi\.twitch\.tv PRIVMSG #([^ ]*) :([^ ]*) just subscribed!",
            ircMessage)
        if match:
            new_subscriber = match.group(2)
            self.logger.info("New subscriber! %s" % new_subscriber)
            for sub in self.sub_subscribers:
                sub(new_subscriber, 0)
            return True
        match = re.search(
            r":twitchnotify!twitchnotify@twitchnotify\.tmi\.twitch\.tv PRIVMSG #([^ ]*) :([^ ]*) subscribed for (.) months in a row!",
            ircMessage)
        if match:
            subscriber = match.group(2)
            months = match.group(3)
            self.logger.info(("%s subscribed for %s months in a row") %
                        (subscriber, months))
            for sub in self.sub_subscribers:
                sub(subscriber, months)
            return True

    def check_ping(self, ircMessage):
        "Respond to ping messages or twitch boots us off"
        if ircMessage.find('PING ') != -1:
            self.logger.info("Responding to a ping from twitch... pong!")
            self.push(str("PING :pong\n").encode('UTF-8'))
            return True

    def check_message(self, ircMessage):
        "Watch for chat messages and notifiy subsribers"
        if ircMessage[0] == "@":
            argstring_regex = r"^@([^ ]*)"
            argstring = re.search(argstring_regex, ircMessage).group(1)
            arg_regx = r"([^=;]*)=([^;]*)"
            args = dict(re.findall(arg_regx, ircMessage))
            regex = r'^@[^ ]* :([^!]*)![^!]*@[^.]*.tmi.twitch.tv'  # username
            regex += r' PRIVMSG #([^ ]*)'  # channel
            regex += r' :(.*)'  # message
            match = re.search(regex, ircMessage)
            args['username'] = match.group(1)
            args['channel'] = match.group(2)
            args['message'] = match.group(3)
            self.logger.info("aaa")
            for subscriber in self.chat_subscribers:
                subscriber(args)
            return True

    def handleIRCMessage(self, ircMessage):
        "Handle incoming IRC messages"
        self.logger.debug(ircMessage)
        self.check_error(ircMessage)
        if self.check_join(ircMessage):
            return
        elif self.check_ping(ircMessage):
            return
        elif self.check_subscriber(ircMessage):
            return
        elif self.check_message(ircMessage):
            return
        else:
            #self.logger.debug(ircMessage)
            pass

    def start(self):
        "Connect start message watching thread"
        if not self.asynloop_thread.is_alive():
            self.running = True
            self.asynloop_thread = Thread(target=self.run)
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connect(('irc.twitch.tv', 6667))
            self.asynloop_thread.start()
        else:
            self.logger.critical("Already running can't run twice")

    def stop(self):
        "Terminate the message watching thread by killing the socket"
        if self.asynloop_thread.is_alive():
            if self.socket:
                self.close()
            self.asynloop_thread.join()

    def run(self):
        "Loop!"
        try:
            asyncore.loop(map=self.map)
        finally:
            self.running = False
