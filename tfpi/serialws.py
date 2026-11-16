"""The Serial Port page's live connection: an asyncio WebSocket server
(python3 -m tfpi.serialws, systemd/tfpi-serial.service).

It pushes what a314d appends to serial.log to every open page as soon as it
is written: inotify wakes it, nothing polls. What a page types comes back
over the same WebSocket and goes to a314d's pty. A page connects with a
short-lived token from the web page (make_token), signed with the web page's
secret key, so only a logged-in browser gets the port.
"""

import argparse
import asyncio
import ctypes
import ctypes.util
import json
import os
from urllib.parse import parse_qs, urlsplit

import websockets
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, serial

PORT = 8081
TOKEN_SALT = "tfpi-serial"
TOKEN_AGE = 120                     # s: the page connects at once, or asks for a new one

# inotify(7)
IN_MODIFY = 0x00000002
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
DIR_EVENTS = IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
PARENT_EVENTS = IN_CREATE | IN_MOVED_TO | IN_DELETE | IN_MOVED_FROM


def make_token(secret):
    return URLSafeTimedSerializer(secret, salt=TOKEN_SALT).dumps("serial")


def check_token(secret, token):
    if not secret or not token:
        return False
    try:
        return URLSafeTimedSerializer(secret, salt=TOKEN_SALT).loads(token, max_age=TOKEN_AGE) == "serial"
    except BadSignature:
        return False


def web_secret():
    return config.read_json(config.WEB_CONF, {}).get("secret_key")


class Inotify:
    """The inotify calls through libc; the fd goes to the event loop."""

    def __init__(self):
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")

    def watch(self, path, mask):
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(str(path)), mask)
        return wd if wd >= 0 else None

    def unwatch(self, wd):
        self._libc.inotify_rm_watch(self.fd, wd)

    def drain(self):
        """Read and drop the queued events: they only say "look again"."""
        while True:
            try:
                if not os.read(self.fd, 4096):
                    return
            except BlockingIOError:
                return


class Bridge:
    def __init__(self, secret=web_secret):
        self.secret = secret            # a function: the key can appear after setup
        self.clients = set()
        self.ino = Inotify()
        self.dir = None
        self.dir_wd = None
        self.offset = -1                # how far the pages have been sent the log
        # the directories a314d may create its own in
        for d in serial.DIRS:
            if d.parent.is_dir():
                self.ino.watch(d.parent, PARENT_EVENTS)
        self._follow()
        if self.dir is not None:
            self.offset = serial.read(-1)[1]

    def _follow(self):
        """Watch a314d's directory (it may appear, move or go)."""
        d = serial.directory()
        if d == self.dir:
            return False
        if self.dir_wd is not None:
            self.ino.unwatch(self.dir_wd)
            self.dir_wd = None
        self.dir = d
        if d is not None:
            self.dir_wd = self.ino.watch(d, DIR_EVENTS)
        return True

    def _send(self, clients, message):
        if clients:
            websockets.broadcast(clients, json.dumps(message))

    def pump(self):
        """Send the pages whatever is new in the log."""
        if self._follow():
            present = self.dir is not None
            self.offset = 0 if present else -1      # a new directory: its log from the start
            self._send(self.clients, {"present": present})
        if self.dir is None:
            return
        while True:
            text, nxt = serial.read(self.offset)
            if text is None:
                return
            self.offset = nxt
            if not text:
                return
            self._send(self.clients, {"data": text})

    def on_inotify(self):
        self.ino.drain()
        self.pump()

    async def handler(self, ws, path=None):
        """One page. websockets 10.x passes the path; 11 and later only the
        connection (path in ws.path, or ws.request.path from 13)."""
        if path is None:
            path = getattr(ws, "path", None) or ws.request.path
        token = parse_qs(urlsplit(path).query).get("t", [""])[0]
        if not check_token(self.secret(), token):
            await ws.close(4001, "log in again")
            return
        # catch up first, then the end of the log and the page in one step
        # (no await between): later output always comes after it
        self.pump()
        text, _ = serial.read(-1)
        self.clients.add(ws)
        self._send({ws}, {"present": text is not None, "data": text or ""})
        try:
            async for message in ws:
                if isinstance(message, str) and message:
                    try:
                        serial.send_raw(message)
                    except OSError as error:
                        self._send({ws}, {"error": str(error)})
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)


async def run(port, ready=None, secret=web_secret):
    bridge = Bridge(secret)
    loop = asyncio.get_running_loop()
    loop.add_reader(bridge.ino.fd, bridge.on_inotify)
    async with websockets.serve(bridge.handler, None, port, max_size=64 * 1024, ping_interval=20) as server:
        if ready is not None:
            ready.set_result(server)
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    asyncio.run(run(args.port))


if __name__ == "__main__":
    main()
