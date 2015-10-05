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

    def __init__(self, user, oauth, channels):
        self.logger = logging.getLogger(name="twitch_chat")
        self.chat_subscribers = []
        self.sub_subscribers = []
        self.channels = channels
        self.user = user
        self.oauth = oauth
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
        self.channel_servers = self.eliminate_duplicate_servers(channel_servers)
        self.irc_handlers = []
        for server in self.channel_servers:
            handler = tmi_client(server,self.handle_message,self.handle_connect)
            self.irc_handlers.append(handler)

    def run(self):
        try:
            for handler in self.irc_handlers:
                handler.start()
            while True:
                time.sleep(0.1)
        finally:
            asyncore.close_all()
            for handler in self.irc_handlers:
                handler.stop()

    def eliminate_duplicate_servers(self, channel_servers):
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

    def subscribeChatMessage(self, callback):
        "Subscribe to a callback for incoming chat messages"
        self.chat_subscribers.append(callback)

    def subscribeNewSubscriber(self, callback):
        "Subscribe to a callback for new subscribers and resubs"
        self.sub_subscribers.append(callback)

    def check_error(self, ircMessage,client):
        "Check for a login error notification and terminate if found"
        if re.search(r":tmi.twitch.tv NOTICE \* :Error logging i.*", ircMessage):
            self.logger.critical(
                "Error logging in to twitch irc, check your oauth and username are set correctly in config.txt!")
            self.stop()

    def check_join(self, ircMessage,client):
        "Watch for successful channel join messages"
        for chan in self.channels:
            if ircMessage.find("JOIN #%s" % chan) != -1:
                self.logger.info("Joined channel %s successfully" % chan)
                return True

    def check_subscriber(self, ircMessage,client):
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
            self.logger.info(("%s subscribed for %s months in a row") % (subscriber, months))
            for sub in self.sub_subscribers:
                sub(subscriber, months)
            return True

    def check_ping(self, ircMessage,client):
        "Respond to ping messages or twitch boots us off"
        if ircMessage.find('PING ') != -1:
            self.logger.info("Responding to a ping from twitch... pong!")
            client.push(str("PING :pong\n").encode('UTF-8'))
            return True

    def check_message(self, ircMessage,client):
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
            for subscriber in self.chat_subscribers:
                subscriber(args)
            return True

    def handle_connect(self,client):
        self.logger.info('Connected..authenticating as %s' % self.user)
        client.push(str('Pass ' + self.oauth + '\r\n').encode('UTF-8'))
        client.push(str('NICK ' + self.user + '\r\n').lower().encode('UTF-8'))
        client.push(str('CAP REQ :twitch.tv/tags\r\n').encode('UTF-8'))
        for server in self.channel_servers:
            if server == client.serverstring:
                self.logger.info('Joining channels %s' % self.channel_servers[server])
                for chan in self.channel_servers[server]:
                    client.push(str('JOIN ' + '#' + chan + '\r\n').encode('UTF-8'))

    def handle_message(self, ircMessage,client):
        "Handle incoming IRC messages"
        self.check_error(ircMessage,client)
        if self.check_join(ircMessage,client):
            return
        elif self.check_ping(ircMessage,client):
            return
        elif self.check_subscriber(ircMessage,client):
            return
        elif self.check_message(ircMessage,client):
            return
        else:
            #self.logger.debug(ircMessage)
            pass


class tmi_client(asynchat.async_chat, object):

    def __init__(self, server, message_callback,connect_callback):
        self.logger = logging.getLogger(name="tmi_client[{0}]".format(server))
        self.logger.info('TMI initializing')
        self.map = {}
        asynchat.async_chat.__init__(self, map=self.map)
        self.received_data = ""
        servernport = server.split(":")
        self.serverstring = server
        self.server = servernport[0]
        self.port = int(servernport[1])
        self.set_terminator('\n')
        self.asynloop_thread = Thread(target=self.run)
        self.running = False
        self.message_callback = message_callback
        self.connect_callback = connect_callback
        self.logger.info('TMI initialized')
        return

    def handle_connect(self):
        "Socket connected successfully"
        self.connect_callback(self)


    def handle_error(self):
        #traceback.print_exc(sys.stderr)
        if self.socket:
            pass
            self.close()
        raise

    def collect_incoming_data(self, data):
        "Dump recieved data into a buffer"
        self.received_data += (data)

    def found_terminator(self):
        "Processes each line of text received from the IRC server."
        txt = self.received_data.rstrip('\r')  #accept RFC-compliant and non-RFC-compliant lines.
        self.received_data = ""
        self.message_callback(txt,self)

    def start(self):
        "Connect start message watching thread"
        if not self.asynloop_thread.is_alive():
            self.running = True
            self.asynloop_thread = Thread(target=self.run)
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connect((self.server, self.port))
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
