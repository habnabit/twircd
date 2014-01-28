from twisted.application.internet import TCPServer
from twisted.application.service import Application
from twisted.internet import reactor
from twisted.web.client import Agent, HTTPConnectionPool

from twircd import TwircFactory

pool = HTTPConnectionPool(reactor)
agent = Agent(reactor, pool=pool)
fac = TwircFactory(reactor, agent)

application = Application('twircd')
TCPServer(6667, fac, interface='::').setServiceParent(application)
