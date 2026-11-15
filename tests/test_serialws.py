"""The Serial Port page's WebSocket (tfpi/serialws.py): tokens, and on Linux
(inotify) the live bridge end to end."""
import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tfpi import serial, serialws

SECRET = "test-secret"


class Tokens(unittest.TestCase):
    def test_round_trip(self):
        token = serialws.make_token(SECRET)
        self.assertTrue(serialws.check_token(SECRET, token))
        self.assertFalse(serialws.check_token("other", token))
        self.assertFalse(serialws.check_token(SECRET, token + "x"))
        self.assertFalse(serialws.check_token(SECRET, ""))
        self.assertFalse(serialws.check_token(None, token))

    def test_expired(self):
        token = serialws.make_token(SECRET)
        with mock.patch.object(serialws, "TOKEN_AGE", -1):
            self.assertFalse(serialws.check_token(SECRET, token))


@unittest.skipUnless(sys.platform.startswith("linux"), "inotify: Linux only")
class Bridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import websockets
        self.ws_mod = websockets
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / "a314d"
        self.dir.mkdir()
        (self.dir / "serial.log").write_bytes(b"~ SSBL ~\n")
        (self.dir / "tty").write_bytes(b"")
        patcher = mock.patch.object(serial, "DIRS", (self.dir,))
        patcher.start()
        self.addCleanup(patcher.stop)
        ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(serialws.run(0, ready, secret=lambda: SECRET))
        server = await asyncio.wait_for(ready, 5)
        # port 0: IPv4 and IPv6 can get different ports; the test uses IPv4
        self.port = next(sock.getsockname()[1] for sock in server.sockets if sock.family == socket.AF_INET)

    async def asyncTearDown(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    def url(self, token):
        return "ws://127.0.0.1:%d/serial?t=%s" % (self.port, token)

    async def next_message(self, ws):
        return json.loads(await asyncio.wait_for(ws.recv(), 2))

    async def test_live_output_and_keys(self):
        async with self.ws_mod.connect(self.url(serialws.make_token(SECRET))) as ws:
            self.assertEqual(await self.next_message(ws), {"present": True, "data": "~ SSBL ~\n"})
            with open(self.dir / "serial.log", "ab") as log:     # as a314d appends
                log.write(b"3.Diag:> \xe4")
            self.assertEqual(await self.next_message(ws), {"data": "3.Diag:> \xe4"})
            await ws.send("dir\r")
            for _ in range(50):
                if (self.dir / "tty").read_bytes():
                    break
                await asyncio.sleep(0.02)
            self.assertEqual((self.dir / "tty").read_bytes(), b"dir\r")

    async def test_log_started_again(self):
        async with self.ws_mod.connect(self.url(serialws.make_token(SECRET))) as ws:
            await self.next_message(ws)
            with open(self.dir / "serial.log", "ab") as log:
                log.write(b"old end\n")
            self.assertEqual(await self.next_message(ws), {"data": "old end\n"})
            (self.dir / "serial.log").rename(self.dir / "serial.log.1")    # a314d at 1 MB
            (self.dir / "serial.log").write_bytes(b"new\n")
            got = ""
            while got != "new\n":
                got += (await self.next_message(ws)).get("data", "")
            self.assertEqual(got, "new\n")

    async def test_bad_token_closed(self):
        async with self.ws_mod.connect(self.url("nope")) as ws:
            with self.assertRaises(self.ws_mod.ConnectionClosed) as caught:
                await asyncio.wait_for(ws.recv(), 2)
        self.assertEqual(caught.exception.code, 4001)


if __name__ == "__main__":
    unittest.main()
