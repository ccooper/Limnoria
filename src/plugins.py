#!/usr/bin/env python

###
# Copyright (c) 2002, Jeremiah Fincher
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
###

import fix

import gc
import os
import re
import sys
import sets
import time
import types
import random
import urllib2
import threading
import cPickle as pickle

import fix
import cdb
import conf
import debug
import utils
import world
import ircdb
import ircutils
import privmsgs
import callbacks

class ChannelDBHandler(object):
    """A class to handle database stuff for individual channels transparently.
    """
    suffix = '.db'
    threaded = False
    def __init__(self, suffix='.db'):
        self.dbCache = ircutils.IrcDict()
        suffix = self.suffix
        if self.suffix and self.suffix[0] != '.':
            suffix = '.' + suffix
        self.suffix = suffix

    def makeFilename(self, channel):
        """Override this to specialize the filenames of your databases."""
        channel = ircutils.toLower(channel)
        prefix = '%s-%s%s' % (channel, self.__class__.__name__, self.suffix)
        return os.path.join(conf.dataDir, prefix)

    def makeDb(self, filename):
        """Override this to create your databases."""
        return cdb.shelf(filename)

    def getDb(self, channel):
        """Use this to get a database for a specific channel."""
        try:
            if self.threaded:
                return self.makeDb(self.makeFilename(channel))
            else:
                return self.dbCache[channel]
        except KeyError:
            db = self.makeDb(self.makeFilename(channel))
            if not self.threaded:
                self.dbCache[channel] = db
            return db

    def die(self):
        for db in self.dbCache.itervalues():
            try:
                db.commit()
            except AttributeError: # In case it's not an SQLite database.
                pass
            try:
                db.close()
            except AttributeError: # In case it doesn't have a close method.
                pass
            del db
        gc.collect()


class PeriodicFileDownloader(object):
    """A class to periodically download a file/files.

    A class-level dictionary 'periodicFiles' maps names of files to
    three-tuples of
    (url, seconds between downloads, function to run with downloaded file).

    'url' should be in some form that urllib2.urlopen can handle (do note that
    urllib2.urlopen handles file:// links perfectly well.)

    'seconds between downloads' is the number of seconds between downloads,
    obviously.  An important point to remember, however, is that it is only
    engaged when a command is run.  I.e., if you say you want the file
    downloaded every day, but no commands that use it are run in a week, the
    next time such a command is run, it'll be using a week-old file.  If you
    don't want such behavior, you'll have to give an error mess age to the user
    and tell him to call you back in the morning.

    'function to run with downloaded file' is a function that will be passed
    a string *filename* of the downloaded file.  This will be some random
    filename probably generated via some mktemp-type-thing.  You can do what
    you want with this; you may want to build a database, take some stats,
    or simply rename the file.  You can pass None as your function and the
    file with automatically be renamed to match the filename you have it listed
    under.  It'll be in conf.dataDir, of course.

    Aside from that dictionary, simply use self.getFile(filename) in any method
    that makes use of a periodically downloaded file, and you'll be set.
    """
    periodicFiles = None
    def __init__(self):
        if self.periodicFiles is None:
            raise ValueError, 'You must provide files to download'
        self.lastDownloaded = {}
        self.downloadedCounter = {}
        for filename in self.periodicFiles:
            if self.periodicFiles[filename][-1] is None:
                fullname = os.path.join(conf.dataDir, filename)
                if os.path.exists(fullname):
                    self.lastDownloaded[filename] = os.stat(fullname).st_ctime
                else:
                    self.lastDownloaded[filename] = 0
            else:
                self.lastDownloaded[filename] = 0
            self.currentlyDownloading = sets.Set()
            self.downloadedCounter[filename] = 0
            self.getFile(filename)

    def _downloadFile(self, filename, url, f):
        infd = urllib2.urlopen(url)
        newFilename = os.path.join(conf.dataDir, utils.mktemp())
        outfd = file(newFilename, 'wb')
        start = time.time()
        s = infd.read(4096)
        while s:
            outfd.write(s)
            s = infd.read(4096)
        infd.close()
        outfd.close()
        msg = 'Downloaded %s in %s seconds' % (filename, time.time() - start)
        debug.msg(msg, 'verbose')
        self.downloadedCounter[filename] += 1
        self.lastDownloaded[filename] = time.time()
        if f is None:
            toFilename = os.path.join(conf.dataDir, filename)
            if os.name == 'nt':
                # Windows, grrr...
                if os.path.exists(toFilename):
                    os.remove(toFilename)
            os.rename(newFilename, toFilename)
        else:
            start = time.time()
            f(newFilename)
            total = time.time() - start
            msg = 'Function ran on %s in %s seconds' % (filename, total)
            debug.msg(msg, 'verbose')
        self.currentlyDownloading.remove(filename)

    def getFile(self, filename):
        (url, timeLimit, f) = self.periodicFiles[filename]
        if time.time() - self.lastDownloaded[filename] > timeLimit and \
           filename not in self.currentlyDownloading:
            debug.msg('Beginning download of %s' % url, 'verbose')
            self.currentlyDownloading.add(filename)
            args = (filename, url, f)
            name = '%s #%s' % (filename, self.downloadedCounter[filename])
            t = threading.Thread(target=self._downloadFile, name=name,
                                 args=(filename, url, f))
            t.setDaemon(True)
            t.start()
            world.threadsSpawned += 1
        

class ConfigurableDictionary(object):
    """This is a dictionary to handle configuration for individual channels,
    including a default configuration for channels that haven't modified their
    configuration from the default.
    """
    def __init__(self, seq):
        self.helps = {}
        self.types = {}
        self.defaults = {}
        self.originalNames = []
        self.channels = ircutils.IrcDict()
        for (name, type, default, help) in seq:
            self.originalNames.append(name)
            name = callbacks.canonicalName(name)
            self.helps[name] = utils.normalizeWhitespace(help)
            self.types[name] = type
            self.defaults[name] = default
        self.originalNames.sort()

    def get(self, name, channel=None):
        name = callbacks.canonicalName(name)
        if channel is not None:
            try:
                return self.channels[channel][name]
            except KeyError:
                return self.defaults[name]
        else:
            return self.defaults[name]

    def set(self, name, value, channel=None):
        name = callbacks.canonicalName(name)
        if channel is not None:
            d = self.channels.setdefault(channel, {})
            d[name] = self.types[name](value)
        else:
            self.defaults[name] = self.types[name](value)

    def help(self, name):
        name = callbacks.canonicalName(name)
        return self.helps[name]

    def names(self):
        return self.originalNames

class ConfigurableTypeError(TypeError):
    pass

def ConfigurableBoolType(s):
    s = s.lower()
    if s in ('true', 'enable', 'on'):
        return True
    elif s in ('false', 'disable', 'off'):
        return False
    else:
        s = 'Value must be one of on/off/true/false/enable/disable.'
        raise ConfigurableTypeError, s

def ConfigurableStrType(s):
    if s and s[0] not in '\'"' and s[-1] not in '\'"':
        s = repr(s)
    try:
        v = utils.safeEval(s)
        if type(v) is not str:
            raise ValueError
    except ValueError:
        raise ConfigurableTypeError, 'Value must be a string.'
    return v

def ConfigurableIntType(s):
    try:
        return int(s)
    except ValueError:
        raise ConfigurableTypeError, 'Value must be an int.'

class Configurable(object):
    """A mixin class to provide a "config" command that can be consistent
    across all plugins, in order to unify the configuration for each plugin.

    Plugins subclassing this should have a "configurables" attribute which is
    a ConfigurableDictionary initialized with a list of 4-tuples of
    (name, type, default, help).  Name is the string name of the config
    variable; type is a function taking a string and returning some value of
    the type the variable is supposed to be; default is the default value the
    variable should take on; help is a string that'll be returned to describe
    the purpose of the config variable.
    """
    def __init__(self):
        className = self.__class__.__name__
        self.filename = os.path.join(conf.confDir,'%s-configurable'%className)
        try:
            if os.path.exists(self.filename):
                configurables = pickle.load(file(self.filename, 'rb'))
                if configurables.names() == self.configurables.names():
                    self.configurables = configurables
        except Exception, e:
            debug.msg('%s raised when trying to unpickle %s configurables' %
                      (debug.exnToString(e), className))

    def die(self):
        pickle.dump(self.configurables, file(self.filename, 'wb'), -1)

    def config(self, irc, msg, args):
        """[<channel>] [<name>] [<value>]

        Sets the value of config variable <name> to <value> on <channel>.  If
        <name> is given but <value> is not, returns the help and current value
        for <name>.  If neither <name> nor <value> is given, returns the valid
        config variables for this plugin.  <channel> is only necessary if the
        message isn't sent in the channel itself.
        """
        try:
            channel = privmsgs.getChannel(msg, args)
            capability = ircdb.makeChannelCapability(channel, 'op')
        except callbacks.ArgumentError:
            raise
        except callbacks.Error:
            channel = None
            capability = 'admin'
        if not ircdb.checkCapability(msg.prefix, capability):
            irc.error(msg, conf.replyNoCapability % capability)
            return
        (name, value) = privmsgs.getArgs(args, required=0, optional=2)
        if not name:
            irc.reply(msg, utils.commaAndify(self.configurables.names()))
            return
        if not value:
            help = self.configurables.help(name)
            value = self.configurables.get(name, channel=channel)
            irc.reply(msg, '%s: %s  (Current value: %r)' % (name, help, value))
            return
        try:
            self.configurables.set(name, value, channel)
            irc.reply(msg, conf.replySuccess)
        except ConfigurableTypeError, e:
            irc.error(msg, str(e))
        

_randomnickRe = re.compile(r'\$randomnick', re.I)
_randomdateRe = re.compile(r'\$randomdate', re.I)
_randomintRe = re.compile(r'\$randomint', re.I)
_channelRe = re.compile(r'\$channel', re.I)
_whoRe = re.compile(r'\$who', re.I)
_botnickRe = re.compile(r'\$botnick', re.I)
_todayRe = re.compile(r'\$today', re.I)
_nowRe = re.compile(r'\$now', re.I)
def standardSubstitute(irc, msg, text):
    """Do the standard set of substitutions on text, and return it"""
    if ircutils.isChannel(msg.args[0]):
        channel = msg.args[0]
    else:
        channel = 'somewhere'
    def randInt(m):
        return str(random.randint(-1000, 1000))
    def randDate(m):
        t = pow(2,30)*random.random()+time.time()/4.0 
        return time.ctime(t)
    def randNick(m):
        if channel != 'somewhere':
            return random.choice(list(irc.state.channels[channel].users))
        else:
            return 'someone'
    text = _channelRe.sub(channel, text)
    text = _randomnickRe.sub(randNick, text)
    text = _randomdateRe.sub(randDate, text)
    text = _randomintRe.sub(randInt, text)
    text = _whoRe.sub(msg.nick, text)
    text = _botnickRe.sub(irc.nick, text)
    text = _todayRe.sub(time.ctime(), text)
    text = _nowRe.sub(time.ctime(), text)
    return text


# vim:set shiftwidth=4 tabstop=8 expandtab textwidth=78:
