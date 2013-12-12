import cgi

import oauth2
from twisted.application.service import MultiService
from twisted.internet import defer, protocol
from twisted.python import log
from twisted.words.protocols import irc

import twits


CONSUMER = oauth2.Consumer(
    'CmGgeNQapcPkgTROF4NKQQ', 'CUaKrjukEFFu9qCavuBWmHiUVgFjsDCR2HgIGNYAKs')


controlEquivalents = dict((i, unichr(0x2400 + i)) for i in xrange(0x20))
controlEquivalents[0x7f] = u'\u2421'
def escapeControls(s):
    return unicode(s).translate(controlEquivalents).encode('utf-8')


class Channel(MultiService):
    def __init__(self, name, twirc, token):
        MultiService.__init__(self)
        self.name = name
        self.twirc = twirc
        self.token = token
        self.tagCounter = 0
        self.tweets = {}
        self.stream = None
        self.settings = {}

    def systemMessage(self, message):
        self.twirc.privmsg('*', self.name, message)

    def _errback(self, failure, context):
        log.err(failure, context)
        self.systemMessage(context + ': ' + failure.getErrorMessage())

    def joined(self):
        self.twirc.join(self.twirc.host, self.name)
        self.twirc.topic(self.twirc.nickname, self.name, None)
        self.twirc.names(self.twirc.nickname, self.name, [self.twirc.nickname])
        self.startService()
        if self.token is not None:
            agent = twits.OAuthAgent(self.twirc.factory.agent, CONSUMER, self.token)
            self.userStreamTwitter = twits.Twitter(agent)
            self.twitter = twits.Twitter(
                agent, streamingAPI='https://stream.twitter.com/1.1/')
            self.command_refresh(None)

    def parted(self):
        self.twirc.part(self.twirc.host, self.name)
        self.stopService()

    def gotMessage(self, message):
        command, _, arg = message.partition(' ')
        method = getattr(self, 'command_' + command, None)
        if method is not None:
            d = defer.maybeDeferred(method, arg)
            d.addErrback(self._errback, 'error in command %r %r' % (command, arg))
        else:
            self.systemMessage('unknown command: %r' % (command,))

    def command_reply(self, arg):
        tag, _, reply = arg.partition(' ')
        tweet = self.tweets[int(tag, 16)]
        at = '@' + tweet['user']['screen_name']
        if at.lower() not in reply.lower():
            reply = at + ' '  + reply
        d = self.twitter.request(
            'statuses/update.json', 'POST',
            status=reply, in_reply_to_status_id=tweet['id'])
        d.addCallback(self._gotTweet)
        return d

    def command_update(self, message):
        d = self.twitter.request('statuses/update.json', 'POST', status=message)
        d.addCallback(self._gotTweet)
        return d

    def command_destroy(self, tag):
        tweet = self.tweets[int(tag, 16)]
        return self.twitter.request('statuses/destroy/%(id)s.json' % tweet, 'POST')

    def command_set(self, arg):
        if not arg:
            for k, v in self.settings.iteritems():
                self.systemMessage('%s: %s' % (k, v))
            return
        setting, _, value = arg.partition(' ')
        if not value:
            self.systemMessage('%s: %s' % (setting, self.settings[setting]))
        else:
            self.settings[setting] = value

    def command_refresh(self, ign):
        if self.stream:
            self.stream.disownServiceParent()
        preserver = twits.StreamPreserver(self.userStreamTwitter, 'user.json', **self.settings)
        preserver.setServiceParent(self)
        preserver.addDelegate(self._gotTweet)
        self.stream = preserver

    def command_timeline(self, user):
        d = self.twitter.request(
            'statuses/user_timeline.json', screen_name=user, count='10', include_rts='true')
        d.addCallback(self._gotTweets)
        return d

    def command_mentions(self, user):
        d = self.twitter.request('statuses/mentions_timeline.json', count='10')
        d.addCallback(self._gotTweets)
        return d

    def _gotTweets(self, tweets):
        for data in reversed(tweets):
            self._gotTweet(data)
        self.systemMessage('end of list')

    def _gotTweet(self, data):
        if 'text' in data:
            user = data['user']['screen_name'].encode('utf-8')
            host = '%s!%s@twitter' % (user, user)
            tag = self.tagCounter
            self.tweets[tag] = data
            self.tagCounter = (self.tagCounter + 1) % 0xfff
            text = escapeControls(twits.extractRealTwitText(data))
            self.twirc.privmsg(host, self.name, '[%x] %s' % (tag, text))
        elif 'delete' in data:
            id_str = data['delete']['status']['id_str']
            tag = next((tag for tag, tweet in self.tweets.iteritems()
                        if tweet['id_str'] == id_str),
                       None)
            if tag is None:
                self.systemMessage('unknown tweet deleted')
            else:
                self.systemMessage('tweet [%x] deleted' % (tag,))
        elif 'started_connecting' in data:
            self.systemMessage('started connecting stream')
        elif 'friends' in data:
            self.systemMessage('stream connected')
        else:
            self.systemMessage(repr(data))


class Twirc(irc.IRC):
    loggedIn = False
    nickname = '*'
    user = realname = None

    def connectionMade(self):
        irc.IRC.connectionMade(self)
        self.channels = {}
        self.tokens = {}
        self._nextMessageDeferred = None

    def send(self, command, *args, **kw):
        args = list(args)
        if args and not kw.get('nocolon'):
            args[-1] = ':' + args[-1]
        self.sendMessage(
            command, self.nickname, *args,
            prefix=kw.get('prefix', self.hostname))

    def irc_NICK(self, prefix, params):
        self.nickname = params[0]
        if not self.loggedIn:
            self._checkLogin()

    def irc_USER(self, prefix, params):
        if not self.loggedIn:
            self.user, _, _, self.realname = params
            self._checkLogin()

    def _checkLogin(self):
        if self.nickname is None or self.user is None:
            return
        self.loggedIn = True
        self.host = '%s!%s@twircd' % (self.nickname, self.user)
        self.send(irc.RPL_WELCOME, 'welcome to twirc')
        self.send(irc.RPL_YOURHOST, 'twircd')
        self.send(irc.RPL_CREATED, 'server created right now, just for you')
        self.send(
            irc.RPL_MYINFO, 'twircd', 'HEAD', '', 't', 'ov', nocolon=True)
        self.addChannel('&twircd')

    def irc_PING(self, prefix, params):
        self.send('PONG', *params)

    def irc_MODE(self, prefix, params):
        if len(params) == 1:
            self.channelMode(self.nickname, params[0], '+t')

    def irc_JOIN(self, prefix, params):
        for channel in params[0].split(','):
            if channel in self.channels:
                continue
            if channel.startswith('#') and channel[1:] in self.tokens:
                self.addChannel(channel, self.tokens[channel[1:]])
                continue
            self.send(irc.ERR_NOSUCHCHANNEL, channel,
                      'invalid channel: %r' % (channel,))

    def irc_PART(self, prefix, params):
        for channel in params[0].split(','):
            if channel not in self.channels:
                self.send(irc.ERR_NOTONCHANNEL, channel, 'not on that channel')
                continue
            channelObj = self.channels.pop(channel)
            channelObj.parted()
            if channel == '&twircd':
                channelObj.joined()

    def irc_QUIT(self, prefix, params):
        self.transport.loseConnection()

    def irc_PRIVMSG(self, prefix, params):
        channel, message = params

        if channel == '&twircd':
            if self._nextMessageDeferred is not None:
                self._nextMessageDeferred.callback(message)
                self._nextMessageDeferred = None
                return

            command, _, arg = message.partition(' ')
            method = getattr(self, 'command_' + command, None)
            if method is not None:
                method(arg)
            else:
                self.systemMessage('unknown command: %r' % (command,))
        elif channel not in self.channels:
            pass
        else:
            channelObj = self.channels[channel]
            channelObj.gotMessage(message)

    def irc_unknown(self, prefix, command, params):
        print `prefix, command, params`

    def getNextMessage(self):
        if self._nextMessageDeferred is not None:
            raise ValueError('already waiting for a message')
        self._nextMessageDeferred = d = defer.Deferred()
        return d

    def addChannel(self, channel, token=None):
        channelObj = self.channels[channel] = Channel(channel, self, token)
        channelObj.joined()

    def systemMessage(self, message):
        self.privmsg('*', '&twircd', message)

    def getToken(self, account=None):
        if account is not None and account in self.tokens:
            return defer.succeed((self.tokens[account], account))

        agent = twits.OAuthAgent(self.factory.agent, CONSUMER)
        twitter = twits.Twitter(
            agent, twitterAPI='https://api.twitter.com/oauth/')
        d = twitter.request('request_token', as_json=False)
        d.addCallback(cgi.parse_qs)

        oauthToken = [None]
        def requestPIN(data):
            oauthToken[0] = data['oauth_token'][0]
            self.systemMessage(
                'please enter the PIN for: '
                'https://api.twitter.com/oauth/authorize?oauth_token=' + oauthToken[0])
            return self.getNextMessage()
        d.addCallback(requestPIN)

        def getFinalToken(pin):
            d = twitter.request(
                'access_token', as_json=False,
                oauth_verifier=pin, oauth_token=oauthToken[0])
            d.addCallback(cgi.parse_qs)
            return d
        d.addCallback(getFinalToken)

        def storeToken(data):
            token = oauth2.Token(data['oauth_token'][0], data['oauth_token_secret'][0])
            self.tokens[data['screen_name'][0]] = token
            return token, data['screen_name'][0]
        d.addCallback(storeToken)

        return d

    def command_add(self, account):
        if account in self.tokens:
            self.systemMessage('account %r already added' % (account,))
        d = self.getToken()
        d.addCallback(self._addedAccount)

    def _addedAccount(self, result):
        token, account = result
        self.systemMessage('got token for %r' % (account,))
        self.addChannel('#' + account, token)


class TwircFactory(protocol.Factory):
    protocol = Twirc

    def __init__(self, agent):
        self.agent = agent
