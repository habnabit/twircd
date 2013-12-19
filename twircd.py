import cgi
import json
import operator
import os

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
        self.tweetsByID = {}
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
        return self.twitter.request('statuses/update.json', 'POST', status=message)

    command_post = command_update

    def command_destroy(self, tag):
        tweet = self.tweets[int(tag, 16)]
        return self.twitter.request('statuses/destroy/%(id)s.json' % tweet, 'POST')

    def command_retweet(self, tag):
        tweet = self.tweets[int(tag, 16)]
        return self.twitter.request('statuses/retweet/%(id)s.json' % tweet, 'POST')

    def command_show(self, tag):
        tweet = self.tweets[int(tag, 16)]
        return self._showTweetTree(tweet)

    command_rt = command_retweet

    def command_url(self, tag):
        tweet = self.tweets[int(tag, 16)]
        message = 'https://twitter.com/user/status/%(id)s' % tweet
        self.systemMessage(message.encode('utf-8'))

    def command_unurl(self, url):
        _, _, status = url.rpartition('/')
        return self._fetchStatus(status)

    def command_set(self, arg):
        if not arg:
            for k, v in self.settings.iteritems():
                self.systemMessage('%s: %s' % (k, v))
            return
        setting, _, value = arg.partition(' ')
        if not value:
            self.systemMessage('%s: %s' % (setting, self.settings[setting]))
        elif value == '%':
            self.settings.pop(setting, None)
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

    def command_search(self, query):
        d = self.twitter.request('search/tweets.json', q=query, count='10', result_type='recent')
        d.addCallback(operator.itemgetter('statuses'))
        d.addCallback(self._gotTweets)
        return d

    def _gotTweets(self, tweets):
        dl = defer.gatherResults(
            [self._gotTweet(data) for data in reversed(tweets)])
        dl.addBoth(lambda ign: self.systemMessage('end of list'))

    def command_msg(self, arg):
        recipient, _, message = arg.partition(' ')
        return self.twitter.request('direct_messages/new.json', method='POST',
                                    screen_name=recipient, text=message)

    def command_follow(self, user):
        return self.twitter.request('friendships/create.json', method='POST',
                                    screen_name=user, follow='true')

    def command_unfollow(self, user):
        return self.twitter.request('friendships/destroy.json', method='POST',
                                    screen_name=user)

    def command_rem(self, ign):
        pass

    def _fetchStatus(self, status, depth=0):
        d = self.twitter.request('statuses/show/%s.json' % (status,))
        d.addCallback(self._gotTweet, depth)
        return d

    def _showTweetTree(self, data, depth=0):
        if depth < 5 and data.get('in_reply_to_status_id'):
            d = self._fetchStatus(data['in_reply_to_status_id'], depth + 1)
            d.addErrback(log.err, 'error fetching reply to %r' % (data,))
            d.addCallback(lambda ign: self._showTweet(data))
            return d
        else:
            self._showTweet(data)
            if depth >= 5:
                self.systemMessage(
                    'not fetching replyee of [%(tag)x]' % data)
        return defer.succeed(None)

    def _showTweet(self, data):
        user = data['user']['screen_name'].encode('utf-8')
        host = '%s!%s@twitter' % (user, user)
        text = escapeControls(twits.extractRealTwitText(data))
        tag = '%(tag)x' % data
        replyee = self.tweetsByID.get(data.get('in_reply_to_status_id'))
        if replyee is not None:
            tag = '%s->%x' % (tag, replyee['tag'])
        self.twirc.privmsg(host, self.name, '[%s] %s' % (tag, text))

    def _gotTweet(self, data, depth=0):
        if 'text' in data:
            if data['id'] in self.tweetsByID:
                return defer.succeed(None)
            tag = data['tag'] = self.tagCounter
            oldTweet = self.tweets.pop(tag, None)
            if oldTweet is not None:
                del self.tweetsByID[oldTweet['id']]
            self.tweets[tag] = data
            self.tweetsByID[data['id']] = data
            self.tagCounter = (self.tagCounter + 1) % 0xfff
            return self._showTweetTree(data, depth)
        elif 'delete' in data:
            tweet = self.tweetsByID.get(data['delete']['status']['id'])
            if tweet is None:
                self.systemMessage('unknown tweet deleted')
            else:
                self.systemMessage('tweet [%(tag)x] deleted' % tweet)
        elif 'direct_message' in data:
            user = data['direct_message']['sender_screen_name'].encode('utf-8')
            recipient = data['direct_message']['recipient_screen_name'].encode('utf-8')
            host = '%s!%s@twitter' % (user, user)
            text = escapeControls(data['direct_message']['text'])
            self.twirc.privmsg(host, self.name, '%s: %s' % (recipient, text))
        elif 'started_connecting' in data:
            self.systemMessage('started connecting stream')
        elif 'friends' in data:
            self.systemMessage('stream connected')
        elif 'event' in data:
            event = data['event']
            if event == 'follow':
                message = '%s has started following %s' % (
                    data['source']['screen_name'], data['target']['screen_name'])
                self.systemMessage(message.encode('utf-8'))
            elif event == 'unfollow':
                message = '%s is no longer following %s' % (
                    data['source']['screen_name'], data['target']['screen_name'])
                self.systemMessage(message.encode('utf-8'))
        else:
            log.msg('from %s: %r' % (self.name, data))
        return defer.succeed(None)


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
        if os.path.exists('tokens'):
            self.command_load(None)

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

    def command_save(self, ign):
        data = {account: {'key': t.key, 'secret': t.secret}
                for account, t in self.tokens.iteritems()}
        with open('tokens', 'wb') as outfile:
            json.dump(data, outfile)
        self.systemMessage('saved')

    def command_load(self, ign):
        with open('tokens') as infile:
            data = json.load(infile)
        self.tokens = {account.encode(): oauth2.Token(**t) for account, t in data.iteritems()}
        self.systemMessage('loaded')

    def command_accounts(self, ign):
        if not self.tokens:
            self.systemMessage('no accounts')
        else:
            for account in self.tokens:
                self.systemMessage('- %s' % (account,))

    def _addedAccount(self, result):
        token, account = result
        self.systemMessage('got token for %r' % (account,))
        self.addChannel('#' + account, token)


class TwircFactory(protocol.Factory):
    protocol = Twirc

    def __init__(self, agent):
        self.agent = agent
