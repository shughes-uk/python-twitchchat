import socket
import re
import logging
from threading import Thread
import asynchat
import asyncore
import json

try:
    # Python 3
    from urllib.request import urlopen
except ImportError:
    # Python 2
    from urllib import urlopen

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
            response = urlopen(url)
            data = json.loads(response.read().decode('UTF-8'))
            for server in data["chat_servers"]:
                if server in channel_servers:
                    channel_servers[server]['channel_set'].add(channel)
                else:
                    channel_servers[server] = {"channel_set": {channel}}
        self.channel_servers = self.eliminate_duplicate_servers(channel_servers)
        self.irc_handlers = []
        for server in self.channel_servers:
            handler = tmi_client(server, self.handle_message, self.handle_connect)
            self.channel_servers[server]['client'] = handler
            self.irc_handlers.append(handler)
        self.pre_messageregex = r"".format(user)

    def start(self):
        for handler in self.irc_handlers:
            handler.start()

    def stop(self):
        for handler in self.irc_handlers:
            handler.stop()

    def eliminate_duplicate_servers(self, channel_servers):
        for key in list(channel_servers):
            if key in channel_servers:
                for other_key in list(channel_servers):
                    if other_key != key and other_key in channel_servers:
                        if channel_servers[other_key]['channel_set'].issubset(channel_servers[key]['channel_set']):
                            del channel_servers[other_key]
        for server in channel_servers:
            for channel in channel_servers[server]['channel_set']:
                for other_server in channel_servers:
                    if other_server != server and channel in channel_servers[other_server]['channel_set']:
                        channel_servers[other_server]['channel_set'].remove(channel)
        return channel_servers

    def subscribeChatMessage(self, callback):
        "Subscribe to a callback for incoming chat messages"
        self.chat_subscribers.append(callback)

    def subscribeNewSubscriber(self, callback):
        "Subscribe to a callback for new subscribers and resubs"
        self.sub_subscribers.append(callback)

    def check_error(self, ircMessage, client):
        "Check for a login error notification and terminate if found"
        if re.search(r":tmi.twitch.tv NOTICE \* :Error logging i.*", ircMessage):
            self.logger.critical(
                "Error logging in to twitch irc, check your oauth and username are set correctly in config.txt!")
            self.stop()
            return True

    def check_join(self, ircMessage, client):
        "Watch for successful channel join messages"
        match = re.search(r":{0}!{0}@{0}\.tmi\.twitch\.tv JOIN #(.*)".format(self.user), ircMessage)
        if match:
            if match.group(1) in self.channels:
                self.logger.info("Joined channel {0} successfully".format(match.group(1)))
                return True

    def check_subscriber(self, ircMessage, client):
        "Parse out new twitch subscriber messages and then call... python subscribers"
        match = re.search(
            r":twitchnotify!twitchnotify@twitchnotify\.tmi\.twitch\.tv PRIVMSG #([^ ]*) :([^ ]*) just subscribed!",
            ircMessage)
        if match:
            channel = match.group(1)
            new_subscriber = match.group(2)
            self.logger.info("{0} has a new subscriber! {1}".format(channel, new_subscriber))
            for sub in self.sub_subscribers:
                sub(channel, new_subscriber, 0)
            return True
        match = re.search(
            r":twitchnotify!twitchnotify@twitchnotify\.tmi\.twitch\.tv PRIVMSG #([^ ]*) :([^ ]*) subscribed for (.) months in a row!",
            ircMessage)
        if match:
            channel = match.group(1)
            subscriber = match.group(2)
            months = match.group(3)
            self.logger.info(("{0} subscribed to {1} for {2} months in a row".format(subscriber, channel, months)))
            for sub in self.sub_subscribers:
                sub(channel, subscriber, months)
            return True

    def check_ping(self, ircMessage, client):
        "Respond to ping messages or twitch boots us off"
        if re.search(r"PING :tmi\.twitch\.tv", ircMessage):
            self.logger.info("Responding to a ping from twitch... pong!")
            client.push(str("PING :pong\r\n").encode('UTF-8'))
            return True

    def check_message(self, ircMessage, client):
        "Watch for chat messages and notifiy subsribers"
        if ircMessage[0] == "@":
            arg_regx = "([^=;]*)=([^ ;]*)"
            arg_regx = re.compile(arg_regx, re.UNICODE)
            args = dict(re.findall(arg_regx, ircMessage[1:]))
            regex = ('^@[^ ]* :([^!]*)![^!]*@[^.]*.tmi.twitch.tv'  # username
                     ' PRIVMSG #([^ ]*)'  # channel
                     ' :(.*)')  # message
            regex = re.compile(regex, re.UNICODE)
            match = re.search(regex, ircMessage)
            args['username'] = match.group(1)
            args['channel'] = match.group(2)
            args['message'] = match.group(3)
            for subscriber in self.chat_subscribers:
                try:
                    subscriber(args)
                except Exception:
                    msg = "Exception during callback to {0}".format(subscriber)
                    self.logger.exception(msg)
            return True

    def handle_connect(self, client):
        self.logger.info('Connected..authenticating as %s' % self.user)
        client.push(str('Pass ' + self.oauth + '\r\n').encode('UTF-8'))
        client.push(str('NICK ' + self.user + '\r\n').lower().encode('UTF-8'))
        client.push(str('CAP REQ :twitch.tv/tags\r\n').encode('UTF-8'))
        for server in self.channel_servers:
            if server == client.serverstring:
                self.logger.info('Joining channels %s' % self.channel_servers[server])
                for chan in self.channel_servers[server]['channel_set']:
                    client.push(str('JOIN ' + '#' + chan.lower() + '\r\n').encode('UTF-8'))

    def handle_message(self, ircMessage, client):
        "Handle incoming IRC messages"
        self.logger.debug(ircMessage)
        if self.check_message(ircMessage, client):
            return
        elif self.check_join(ircMessage, client):
            return
        elif self.check_subscriber(ircMessage, client):
            return
        elif self.check_ping(ircMessage, client):
            return
        elif self.check_error(ircMessage, client):
            return

    def send_message(self, channel, message):
        for server in self.channel_servers:
            if channel in self.channel_servers[server]['channel_set']:
                client = self.channel_servers[server]['client']
                client.push(str('PRIVMSG #%s :%s\n' % (channel, message)).encode('UTF-8'))
                break


class tmi_client(asynchat.async_chat, object):

    def __init__(self, server, message_callback, connect_callback):
        self.logger = logging.getLogger(name="tmi_client[{0}]".format(server))
        self.logger.info('TMI initializing')
        self.map = {}
        asynchat.async_chat.__init__(self, map=self.map)
        self.received_data = bytearray()
        servernport = server.split(":")
        self.serverstring = server
        self.server = servernport[0]
        self.port = int(servernport[1])
        self.set_terminator(b'\n')
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
        if self.socket:
            self.close()
        raise

    def collect_incoming_data(self, data):
        "Dump recieved data into a buffer"
        self.received_data += data

    def found_terminator(self):
        "Processes each line of text received from the IRC server."
        txt = self.received_data.rstrip(b'\r')  # accept RFC-compliant and non-RFC-compliant lines.
        self.received_data.clear()
        self.message_callback(txt.decode("utf-8"), self)

    def start(self):
        "Connect start message watching thread"
        if not self.asynloop_thread.is_alive():
            self.running = True
            self.asynloop_thread = Thread(target=self.run)
            self.asynloop_thread.daemon = True
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
            try:
                self.asynloop_thread.join()
            except RuntimeError as e:
                if e.message == "cannot join current thread":
                    # this is thrown when joining the current thread and is ok.. for now"
                    pass
                else:
                    raise e

    def run(self):
        "Loop!"
        try:
            asyncore.loop(map=self.map)
        finally:
            self.running = False
