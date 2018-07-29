import asynchat
import asyncore
import json
import logging
import re
import socket
import sys
import time
from datetime import datetime, timedelta
from threading import Thread

PY3 = sys.version_info[0] == 3
if PY3:
    from urllib.request import urlopen, Request
    from queue import Queue
else:
    from urllib2 import urlopen, Request
    from Queue import Queue

logger = logging.getLogger(name="tmi")


class twitch_chat(object):

    def __init__(self, user, oauth, channels, client_id):
        self.logger = logging.getLogger(name="twitch_chat")
        self.chat_subscribers = []
        self.usernotice_subscribers = []
        self.channels = channels
        self.user = user
        self.oauth = oauth
        self.channel_servers = {'irc.chat.twitch.tv:6667': {'channel_set': channels}}
        self.irc_handlers = []
        for server in self.channel_servers:
            handler = tmi_client(server, self.handle_message, self.handle_connect)
            self.channel_servers[server]['client'] = handler
            self.irc_handlers.append(handler)

    def start(self):
        for handler in self.irc_handlers:
            handler.start()

    def join(self):
        for handler in self.irc_handlers:
            handler.asynloop_thread.join()

    def stop(self):
        for handler in self.irc_handlers:
            handler.stop()

    def subscribeChatMessage(self, callback):
        "Subscribe to a callback for incoming chat messages"
        self.chat_subscribers.append(callback)

    def subscribeUsernotice(self, callback):
        "Subscribe to a callback for new subscribers and resubs"
        self.usernotice_subscribers.append(callback)

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

    def check_usernotice(self, ircMessage, client):
        "Parse out new twitch subscriber messages and then call... python subscribers"
        if ircMessage[0] == '@':
            arg_regx = r"([^=;]*)=([^ ;]*)"
            arg_regx = re.compile(arg_regx, re.UNICODE)
            args = dict(re.findall(arg_regx, ircMessage[1:]))
            regex = (
                r'^@[^ ]* :tmi.twitch.tv'
                r' USERNOTICE #(?P<channel>[^ ]*)'  # channel
                r'((?: :)?(?P<message>.*))?')  # message
            regex = re.compile(regex, re.UNICODE)
            match = re.search(regex, ircMessage)
            if match:
                args['channel'] = match.group(1)
                args['message'] = match.group(2)
                for subscriber in self.usernotice_subscribers:
                    try:
                        subscriber(args)
                    except Exception:
                        msg = "Exception during callback to {0}".format(subscriber)
                        self.logger.exception(msg)
                return True

    def check_ping(self, ircMessage, client):
        "Respond to ping messages or twitch boots us off"
        if re.search(r"PING :tmi\.twitch\.tv", ircMessage):
            self.logger.info("Responding to a ping from twitch... pong!")
            client.send_message("PING :pong\r\n")
            return True

    def check_message(self, ircMessage, client):
        "Watch for chat messages and notifiy subsribers"
        if ircMessage[0] == "@":
            arg_regx = r"([^=;]*)=([^ ;]*)"
            arg_regx = re.compile(arg_regx, re.UNICODE)
            args = dict(re.findall(arg_regx, ircMessage[1:]))
            regex = (r'^@[^ ]* :([^!]*)![^!]*@[^.]*.tmi.twitch.tv'  # username
                     r' PRIVMSG #([^ ]*)'  # channel
                     r' :(.*)')  # message
            regex = re.compile(regex, re.UNICODE)
            match = re.search(regex, ircMessage)
            if match:
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
        self.logger.info('Connected..authenticating as {0}'.format(self.user))
        client.send_message('Pass ' + self.oauth + '\r\n')
        client.send_message('NICK ' + self.user + '\r\n'.lower())
        client.send_message('CAP REQ :twitch.tv/tags\r\n')
        client.send_message('CAP REQ :twitch.tv/membership\r\n')
        client.send_message('CAP REQ :twitch.tv/commands\r\n')

        for server in self.channel_servers:
            if server == client.serverstring:
                self.logger.info('Joining channels {0}'.format(self.channel_servers[server]))
                for chan in self.channel_servers[server]['channel_set']:
                    client.send_message('JOIN ' + '#' + chan.lower() + '\r\n')

    def handle_message(self, ircMessage, client):
        "Handle incoming IRC messages"
        self.logger.debug(ircMessage)
        if self.check_message(ircMessage, client):
            return
        elif self.check_join(ircMessage, client):
            return
        elif self.check_usernotice(ircMessage, client):
            return
        elif self.check_ping(ircMessage, client):
            return
        elif self.check_error(ircMessage, client):
            return

    def send_message(self, channel, message):
        for server in self.channel_servers:
            if channel in self.channel_servers[server]['channel_set']:
                client = self.channel_servers[server]['client']
                client.send_message(u'PRIVMSG #{0} :{1}\n'.format(channel, message))
                break


MAX_SEND_RATE = 20
SEND_RATE_WITHIN_SECONDS = 30


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
        self.message_queue = Queue()
        self.messages_sent = []
        self.logger.info('TMI initialized')
        return

    def send_message(self, msg):
        self.message_queue.put(msg.encode("UTF-8"))

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
        del self.received_data[:]
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

            self.send_thread = Thread(target=self.send_loop)
            self.send_thread.daemon = True
            self.send_thread.start()
        else:
            self.logger.critical("Already running can't run twice")

    def stop(self):
        "Terminate the message watching thread by killing the socket"
        self.running = False
        if self.asynloop_thread.is_alive():
            if self.socket:
                self.close()
            try:
                self.asynloop_thread.join()
                self.send_thread.join()
            except RuntimeError as e:
                if e.message == "cannot join current thread":
                    # this is thrown when joining the current thread and is ok.. for now"
                    pass
                else:
                    raise e

    def send_loop(self):
        while self.running:
            time.sleep(1)
            if len(self.messages_sent) < MAX_SEND_RATE:
                if not self.message_queue.empty():
                    to_send = self.message_queue.get()
                    self.logger.debug("Sending")
                    self.logger.debug(to_send)
                    self.push(to_send)
                    self.messages_sent.append(datetime.now())
            else:
                time_cutoff = datetime.now() - timedelta(seconds=SEND_RATE_WITHIN_SECONDS)
                self.messages_sent = [dt for dt in self.messages_sent if dt < time_cutoff]

    def run(self):
        "Loop!"
        try:
            asyncore.loop(map=self.map)
        finally:
            self.running = False
