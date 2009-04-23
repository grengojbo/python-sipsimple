#!/usr/bin/env python
# Copyright (C) 2008-2009 AG Projects. See LICENSE for details.
#

from __future__ import with_statement
import sys
import os
import datetime
import time
from collections import deque
from twisted.internet.error import ConnectionClosed

from eventlet import api, coros, proc
from eventlet.green.socket import gethostbyname
from msrplib import connect
from msrplib.session import ConnectionClosedErrors
from msrplib import trafficlog
from msrplib.protocol import URI

from sipsimple.core import Credentials, SDPSession, SDPConnection, SIPURI, SIPCoreError, WaveFile, PJSIPError, Route
from sipsimple.clients.console import setup_console, CTRL_D, EOF
from sipsimple.green.core import GreenEngine, IncomingSessionHandler, Ringer, GreenInvitation, GreenRegistration
from sipsimple.green.sessionold import MSRPSession, MSRPSessionErrors, IncomingMSRPHandler, make_SDPMedia
from sipsimple.clients.config import parse_options, update_options, get_download_path, get_history_file, get_credentials
from sipsimple.clients.clientconfig import get_path
from sipsimple.clients import enrollment, format_cmdline_uri
from sipsimple.clients.cpim import MessageCPIMParser, SIPAddress
from sipsimple.clients.sdputil import FileSelector
from sipsimple import logstate

enrollment.verify_account_config()

PJSIP_EINVALIDURI = 171039

KEY_NEXT_SESSION = '\x0e' # Ctrl-N

trafficlog.hook_std_output()

incoming = coros.queue()

class UserCommandError(Exception):
    pass

def format_display_user_host(display, user, host):
    if display:
        return '%s (%s@%s)' % (display, user, host)
    else:
        return '%s@%s' % (user, host)

def format_uri(sip_uri, cpim_uri=None):
    if cpim_uri is not None:
        if (sip_uri.host, sip_uri.user) == (cpim_uri.host, cpim_uri.user):
            return format_display_user_host(cpim_uri.display or sip_uri.display, sip_uri.user, sip_uri.host)
        else:
            # conference, pasting only header from cpim
            return format_display_user_host(cpim_uri.display, cpim_uri.user, cpim_uri.host)
    return format_display_user_host(sip_uri.display, sip_uri.user, sip_uri.host)

def format_datetime(dt):
    """Format time in the local timezone.
    dt is datetime with tzinfo = UTC (or None which will be treated like UTC).

    >>> from sipsimple.clients.iso8601 import parse_date
    >>> time.timezone == -6*60*60 # this test can only be executed in Novosibirsk
    True
    >>> format_datetime(parse_date('2009-02-03T14:30:04'))
    '20:30:04'
    """
    if dt.tzinfo is None or not dt.tzinfo.utcoffset(dt):
        dt -= datetime.timedelta(seconds=time.timezone)
        if dt.date()==datetime.date.today():
            return dt.strftime('%X')
        else:
            return dt.strftime('%X %x')
    else:
        return repr(dt)

def format_incoming_message(uri, message):
    if message.content_type == 'message/cpim':
        headers, text = MessageCPIMParser.parse_string(message.data)
        cpim_uri = headers.get('From')
        dt = headers.get('DateTime')
    else:
        cpim_uri = None
        if message.content_type == 'text/plain':
            text = message.data
        else:
            text = repr(message)
        dt = None
    if dt is None:
        return '%s: %s' % (format_uri(uri, cpim_uri), text)
    else:
        return '%s %s: %s' % (format_datetime(dt), format_uri(uri, cpim_uri), text)

def format_nosessions_ps(myuri):
    return '%s@%s> ' % (myuri.user, myuri.host)

def format_outgoing_message(uri, message, dt):
    return '%s %s: %s' % (format_datetime(dt), format_uri(uri), message)

def forward_chunks(msrp, listener, tag):
    while True:
        try:
            result = msrp.receive_chunk()
        except ConnectionClosedErrors, e:
            break
        else:
            listener.send((tag, result))
# forward covers bug in the design. instead put listener directly at source

class ChatSession(object):
    """Represents either an existing MSRPSession or invite-in-progress that
    will soon produce MSRPSession.

    Until invite is completed send_message works but piles up messages in a queue
    that will be emptied upon session establishment.
    """

    def __init__(self, sip, msrpsession=None, invite_job=None):
        self.sip = sip
        self.msrpsession = msrpsession
        self.invite_job = invite_job
        self.messages_to_send = deque()
        self.source = proc.Source()
        self.history_file = None
        if self.invite_job is not None:
            self.invite_job.link_value(lambda p: proc.spawn(self._on_invite, p.value))
            self.invite_job.link_exception(self.source)
        else:
            self.start_rendering_messages()
        self.forwarder = None
        self.source.link(lambda *_: proc.spawn_greenlet(self.end))

    def link(self, listener):
        """Add a listener to be notified when either msrpsession dies or invite fails"""
        return self.source.link(listener)

    @staticmethod
    def _do_invite(inv, msrp_connector, make_SDPMedia, ringer, target_uri, local_uri):
        try:
            return MSRPSession.invite(inv, msrp_connector, make_SDPMedia, ringer, local_uri)
        except MSRPSessionErrors, ex:
            print 'Connection to %s FAILED: %s' % (target_uri, ex)
            return ex
        except PJSIPError, ex:
            if ex.status == PJSIP_EINVALIDURI:
                print ex.message
            else:
                raise
            return ex

    def _on_invite(self, result):
        if isinstance(result, Exception):
            self.source.send_exception(result)
        else:
            self.msrpsession = result
            self.start_rendering_messages()
            for message in self.messages_to_send:
                self.send_message(*message)
            del self.messages_to_send

    @classmethod
    def invite(cls, inv, msrp_connector, make_SDPMedia, ringer, target_uri, local_uri):
        invite_job=proc.spawn(cls._do_invite, inv, msrp_connector, make_SDPMedia, ringer, target_uri, local_uri)
        return cls(inv, invite_job=invite_job)

    def start_rendering_messages(self):
        self.history_file = get_history_file(self.sip)
        self.forwarder = proc.spawn(forward_chunks, self.msrpsession.msrp, incoming, self)
        self.msrpsession.link(self.source)

    def end(self):
        if self.forwarder:
            self.forwarder.kill()
        if self.invite_job:
            self.invite_job.kill()
        if self.msrpsession is not None:
            self.msrpsession.end()
        if self.history_file:
            self.history_file.close()
            self.history_file = None

    def send_message(self, msg, content_type=None, dt=None):
        if dt is None:
            dt = datetime.datetime.utcnow()
        if self.msrpsession is None:
            if not self.invite_job:
                raise AssertionError('This session is dead; do not send messages there')
            self.messages_to_send.append((msg, content_type, dt))
            print 'Message will be delivered once connection is established'
        else:
            printed_msg = format_outgoing_message(self.sip.local_uri, msg, dt)
            print printed_msg
            self.history_file.write(printed_msg + '\n')
            self.history_file.flush()
            return self.msrpsession.send_message(msg, content_type, datetime_=dt)

    def format_ps(self):
        return 'Chat to %s: ' % format_uri(self.sip.remote_uri)


def consult_user(inv, ask_func):
    """Ask the user about the invite. Return True if the user has accepted it.
    Otherwise end the session with the appropriate error response.

    To actually request user's input `ask_func' is run in a separate greenlet.
    It must return True if user selected 'Accept' or False if user has selected
    'Reject'. This greenlet maybe killed if the session was closed. In that
    case it should exit immediatelly, because consult_user won't exit until
    it finishes.
    """
    ask_job = proc.spawn_link_exception(proc.wrap_errors(proc.ProcExit, ask_func), inv)
    link = inv.call_on_disconnect(lambda *_args: ask_job.kill())
    ERROR = 488 # Not Acceptable Here
    try:
        response = ask_job.wait()
        if response == True:
            ERROR = None
            return True
        elif response == False:
            ERROR = 486 # Busy Here
        # note, that response may also be ProcExit instance
    finally:
        link.cancel()
        if ERROR is not None:
            proc.spawn_greenlet(inv.disconnect, ERROR)


class IncomingMSRPHandler_Interactive(IncomingMSRPHandler):

    def _ask_user(self, inv):
        raise NotImplementedError

    def handle(self, inv, *args, **kwargs):
        if consult_user(inv, self._ask_user)==True:
            return IncomingMSRPHandler.handle(self, inv, *args, **kwargs)

class SilentRinger:

    def start(self):
        pass

    def stop(self):
        pass

class IncomingChatHandler(IncomingMSRPHandler_Interactive):

    def __init__(self, acceptor, console, session_factory, ringer=None):
        IncomingMSRPHandler.__init__(self, acceptor, session_factory)
        self.console = console
        if ringer is None:
            ringer = SilentRinger()
        self.ringer = ringer

    def is_acceptable(self, inv):
        if not IncomingMSRPHandler.is_acceptable(self, inv):
            return False
        attrs = inv._attrdict
        if 'sendonly' in attrs:
            return False
        if 'recvonly' in attrs:
            return False
        accept_types = attrs.get('accept-types', '')
        if 'message/cpim' not in accept_types and '*' not in accept_types:
            return False
        wrapped_types = attrs.get('accept-wrapped-types', '')
        if 'text/plain' not in wrapped_types and '*' not in wrapped_types:
            return False
        return True

    def _ask_user(self, inv):
        q = 'Incoming SIP request request from %s, do you accept? (y/n) ' % (inv.caller_uri, )
        inv.respond_to_invite_provisionally()
        self.ringer.start()
        try:
            return self.console.ask_question(q, list('yYnN') + [CTRL_D]) in 'yY'
        finally:
            self.ringer.stop()

    def make_local_SDPSession(self, inv, full_local_path, local_ip):
        return SDPSession(local_ip,
                          connection=SDPConnection(local_ip),
                          media=[make_SDPMedia(full_local_path, ["text/plain"])]) # XXX why text/plain?


class IncomingFileTransferHandler(IncomingMSRPHandler_Interactive):

    def __init__(self, get_acceptor, console, session_factory, ringer=SilentRinger(), auto_accept=False):
        IncomingMSRPHandler.__init__(self, get_acceptor, session_factory)
        self.console = console
        self.ringer = ringer
        self.auto_accept = auto_accept

    def is_acceptable(self, inv):
        if not IncomingMSRPHandler.is_acceptable(self, inv):
            return False
        attrs = inv._attrdict
        if 'sendonly' not in attrs:
            return False
        if 'recvonly' in attrs:
            return False
        inv.file_selector = FileSelector.parse(inv._attrdict['file-selector'])
        return True

    def _format_fileinfo(self, inv):
        attrs = inv._attrdict
        return str(FileSelector.parse(attrs['file-selector']))

    def _ask_user(self, inv):
        if self.auto_accept:
            return True
        q = 'Incoming file transfer %s from %s, do you accept? (y/n) ' % (self._format_fileinfo(inv), inv.caller_uri)
        inv.respond_to_invite_provisionally()
        self.ringer.start()
        try:
            return self.console.ask_question(q, list('yYnN') + [CTRL_D]) in 'yY'
        finally:
            self.ringer.stop()

    def make_local_SDPSession(self, inv, full_local_path, local_ip):
        return SDPSession(local_ip, connection=SDPConnection(local_ip),
                          media=[make_SDPMedia(full_local_path, ["text/plain"])]) # XXX fix content-type


class DownloadFileSession(object):

    def __init__(self, msrpsession):
        self.msrpsession = msrpsession
        proc.spawn(self._reader)

    @property
    def sip(self):
        return self.msrpsession.sip

    @property
    def fileselector(self):
        return FileSelector.parse(self.sip._attrdict['file-selector'])

    def _reader(self):
        chunk = self.msrpsession.msrp.receive_chunk()
        self._save_file(chunk)
        self.msrpsession.end()

    def _save_file(self, message):
        fro, to, length = message.headers['Byte-Range'].decoded
        assert len(message.data)==length, (len(message.data), length) # check MSRP integrity
        if message.content_type=='message/cpim':
            headers, data = MessageCPIMParser.parse_string(message.data)
        else:
            data = message.data
        # check that SIP filesize and MSRP size match
        assert self.fileselector.size == len(data), (self.fileselector.size, len(data))
        path = get_download_path(self.fileselector.name)
        print 'Saving %s to %s' % (self.fileselector, path)
        assert not os.path.exists(path), path # get_download_path must return a new path
        file(path, 'w+').write(data)


class ChatManager:

    def __init__(self, engine, sound, credentials, console, logger, auto_accept_files=False, route=None, relay=None, msrp_tls=True):
        self.engine = engine
        self.sound = sound
        self.credentials = credentials
        self.default_domain = credentials.uri.host
        self.console = console
        self.logger = logger
        self.auto_accept_files = auto_accept_files
        self.route = route
        self.relay = relay
        self.msrp_tls = msrp_tls
        self.sessions = []
        self.downloads = []
        self.accept_incoming_worker = None
        self.current_session = None
        self.message_renderer_job = proc.spawn_link_exception(self._message_renderer)
        self.outbound_ringer = Ringer(self.sound.play, "ring_outbound.wav")

    # is there a need for special process for it? just calling the function is good enough
    def _message_renderer(self):
        try:
            while True:
                chat, chunk = incoming.wait()
                try:
                    msg = format_incoming_message(chat.sip.remote_uri, chunk)
                    print msg
                except ValueError:
                    print 'Failed to parse incoming message, content_type=%r, data=%r' % (chunk.content_type, chunk.data)
                    # XXX: issue REPORT here
                else:
                    chat.history_file.write(msg + '\n')
                    chat.history_file.flush()
                self.sound.play("message_received.wav")
        except proc.ProcExit:
            pass

    def close_current_session(self):
        if self.current_session is not None:
            proc.spawn_greenlet(self.current_session.end)
            self.remove_session(self.current_session)

    def update_ps(self):
        if self.current_session:
            prefix = ''
            if len(self.sessions)>1:
                prefix = '%s/%s ' % (1+self.sessions.index(self.current_session), len(self.sessions))
            ps = prefix + self.current_session.format_ps()
        else:
            ps = format_nosessions_ps(self.credentials.uri)
        self.console.set_prompt(ps)

    def add_session(self, session, activate=True):
        assert session is not None
        self.sessions.append(session)
        session.link(lambda *args: self.remove_session(session))
        if activate:
            self.current_session = session
            # XXX could be asking user a question about another incoming, at this moment
            self.update_ps()

    def remove_session(self, session):
        if session is None:
            return
        try:
            index = self.sessions.index(session)
        except ValueError:
            pass
        else:
            del self.sessions[index]
            if self.sessions:
                if self.current_session is session:
                    self.current_session = self.sessions[index % len(self.sessions)]
            else:
                self.current_session = None
        self.update_ps()

    def add_download(self, session):
        assert session is not None
        self.downloads.append(session)
        session.msrpsession.sip.call_on_disconnect(lambda *args: self.remove_download(session))

    def remove_download(self, session):
        if session is None:
            return
        try:
            self.downloads.remove(session)
        except ValueError:
            pass

    def switch(self):
        if len(self.sessions)<2:
            print "There's no other session to switch to."
        else:
            index = 1+self.sessions.index(self.current_session)
            self.current_session = self.sessions[index % len(self.sessions)]
            self.update_ps()

    def call(self, *args):
        if len(args)!=1:
            raise UserCommandError('Please provide uri')
        target_uri = args[0]
        if not isinstance(target_uri, SIPURI):
            try:
                target_uri = self.engine.parse_sip_uri(format_cmdline_uri(target_uri, self.credentials.uri.host))
            except ValueError, ex:
                raise UserCommandError(str(ex))
        route = self.route
        if route is None:
            route = Route(gethostbyname(target_uri.host or self.credentials.uri.host), target_uri.port or 5060)
        inv = GreenInvitation(self.credentials, target_uri, route=route)
        # XXX should use relay if ti was provided; actually, 2 params needed incoming_relay, outgoing_relay
        msrp_connector = connect.get_connector(None, logger=self.logger)
        local_uri = URI(use_tls=self.msrp_tls)
        chatsession = ChatSession.invite(inv, msrp_connector, self.make_SDPMedia, self.outbound_ringer, target_uri, local_uri)
        self.add_session(chatsession)

    def spawn_link_accept_incoming(self):
        assert not self.accept_incoming_worker, self.accept_incoming_worker
        handler = IncomingSessionHandler()
        inbound_ringer = Ringer(self.sound.play, "ring_inbound.wav")
        def new_chat_session(sip, msrp):
            msrpsession = MSRPSession(sip, msrp)
            chatsession = ChatSession(sip, msrpsession)
            self.add_session(chatsession)
        def new_receivefile_session(sip, msrp):
            msrpsession = MSRPSession(sip, msrp)
            downloadsession = DownloadFileSession(msrpsession)
            self.add_download(downloadsession)
        def get_acceptor():
            return connect.get_acceptor(self.relay, logger=self.logger)
        file = IncomingFileTransferHandler(get_acceptor, self.console,
                                           new_receivefile_session, inbound_ringer,
                                           auto_accept=self.auto_accept_files)
        handler.add_handler(file)
        chat = IncomingChatHandler(get_acceptor, self.console, new_chat_session, inbound_ringer)
        handler.add_handler(chat)
        func = proc.wrap_errors(proc.ProcExit, self._accept_incoming_loop)
        self.accept_incoming_worker = proc.spawn_link_exception(func, handler)

    def stop_accept_incoming(self):
        if self.accept_incoming_worker:
            self.accept_incoming_worker.kill()

    def _accept_incoming_loop(self, handler):
        credentials = get_credentials() if self.msrp_tls else None
        local_uri = URI(port=0, use_tls=self.msrp_tls, credentials=credentials)
        with self.engine.linked_incoming() as q:
            while True:
                inv = q.wait()
                proc.spawn(handler.handle, inv, local_uri=local_uri)

    @staticmethod
    def make_SDPMedia(uri_path):
        return make_SDPMedia(uri_path, ['message/cpim'], ['text/plain'])

    def send_message(self, message):
        session = self.current_session
        if not session:
            raise UserCommandError('No active session')
        try:
            if session.send_message(message):
                #print 'sent %s %s' % (session, message)
                self.sound.play("message_sent.wav")
                return True # indicate that the message was sent
        except ConnectionClosed, ex:
            proc.spawn(self.remove_session, session)
            raise UserCommandError(str(ex))

#    for x in ['end_sip', 'end_msrp']:
#        exec "%s = _helper(%r)" % (x, x)
#
#    del x

def start(options, console):
    ###console.disable()
    engine = GreenEngine()
    engine.start(not options.disable_sound,
                 trace_sip=options.trace_sip,
                 ec_tail_length=0,
                 local_ip=options.local_ip,
                 local_udp_port=options.local_port)
    registration = None
    try:
        update_options(options, engine)
        logstate.start_loggers(trace_pjsip=options.trace_pjsip,
                               trace_engine=options.trace_engine)
        credentials = Credentials(options.uri, options.password)
        logger = trafficlog.Logger(fileobj=console, is_enabled_func=lambda: options.trace_msrp)
        ###console.enable()
        if options.register:
            registration = GreenRegistration(credentials, route=options.route)
            proc.spawn_greenlet(registration.register)
        console.set_prompt(str(options.uri).replace('sip:', '') + '> ')
        sound = ThrottlingSoundPlayer()
        manager = ChatManager(engine, sound, credentials, console, logger,
                              options.auto_accept_files,
                              route=options.route,
                              relay=options.relay,
                              msrp_tls=options.msrp_tls)
        try:
            manager.spawn_link_accept_incoming()
            print "Press Ctrl-d to quit or Control-n to switch between active sessions"
            if not options.args:
                print 'Waiting for incoming SIP session requests...'
            else:
                for x in options.args:
                    manager.call(x)
            while True:
                try:
                    readloop(console, manager, get_commands(manager), get_shortcuts(manager))
                except EOF:
                    if manager.current_session:
                        manager.close_current_session()
                    else:
                        raise
        finally:
            console.copy_input_line()
            manager.stop_accept_incoming()
            if registration is not None:
                registration = proc.spawn(registration.unregister)
            proc.waitall([proc.spawn(session.end) for session in manager.sessions], trap_errors=True)
            if registration is not None:
                registration.wait()
    finally:
        engine.stop()

def get_commands(manager):
    return {#'end sip': manager.end_sip,
            #'end msrp': manager.end_msrp,
            'switch': manager.switch,
            'call': manager.call}

def get_shortcuts(manager):
    return {KEY_NEXT_SESSION: manager.switch}

def readloop(console, manager, commands, shortcuts):
    console.terminalProtocol.send_keys.extend(shortcuts.keys())
    for type, value in console:
        if type == 'key':
            key = value[0]
            if key in shortcuts:
                shortcuts[key]()
        elif type == 'line':
            echoed = []
            def echo():
                """Echo user's input line, once. Note, that manager.send_message() may do echo
                itself (it indicates if it did it in the return value).
                """
                if not echoed:
                    console.copy_input_line(value)
                    echoed.append(1)
            try:
                if value.startswith(':') and value[1:].split()[0] in commands:
                    echo()
                    args = value[1:].split()
                    command = commands[args[0]]
                    command(*args[1:])
                else:
                    if value:
                        if manager.send_message(value):
                            echoed.append(1)
            except UserCommandError, ex:
                echo()
                print ex
            # will get there without echoing if user pressed enter on an empty line; let's echo it
            echo()


class ThrottlingSoundPlayer:

    LIMIT = 2
    VOLUME = 25

    def __init__(self):
        self.cache = {} # { filename: (WaveFile instance, last time it played) }

    def play(self, filename):
        try:
            wavefile, last_time = self.cache[filename]
        except KeyError:
            wavefile = WaveFile(get_path(filename))
            last_time = 0
        current = time.time()
        if current - last_time > self.LIMIT:
            if last_time:
                wavefile.stop()
            last_time = current
            wavefile.start(self.VOLUME)
            self.cache[filename] = (wavefile, last_time)


description = "This script will either sit idle waiting for an incoming MSRP session, or start a MSRP session with the specified target SIP address. The program will close the session and quit when CTRL+D is pressed."
usage = "%prog [options] [target-user@target-domain.com]"

def main():
    try:
        options = parse_options(usage, description)
        with setup_console() as console:
            start(options, console)
    except EOF:
        pass
    except proc.LinkedExited, err:
        print 'Exiting because %s' % (err, )
    except (RuntimeError, SIPCoreError), e:
        sys.exit(str(e) or str(type(e)))

if __name__ == "__main__":
    main()
