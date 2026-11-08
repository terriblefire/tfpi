import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import Sandbox, full_image, jed, make_zip
from tfpi import config, feed

PATH = "terriblefire/tf4060-firmware"


class FakeGhcr:
    """Like ghcr.io: anonymous bearer token, blobs redirected to a storage host
    that refuses the registry token."""

    def __init__(self):
        self.blobs, self.tags = {}, {}
        self.private = False
        self.corrupt = False
        self.missing = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body=b"", headers=None):
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = f"http://127.0.0.1:{self.server.server_port}"
                if self.path.startswith("/token"):
                    if fake.missing:          # ghcr: 403 package not there, 401 private
                        return self.reply(fake.missing, b'{"errors":[{"code":"DENIED"}]}')
                    return self.reply(200, json.dumps({"token": "anon"}).encode())
                if self.path.startswith("/storage/"):
                    if self.headers.get("Authorization"):
                        return self.reply(400, b"only one auth mechanism allowed")
                    data = fake.blobs.get(self.path.rsplit("/", 1)[1])
                    if data is None:
                        return self.reply(404)
                    return self.reply(200, data[:-1] + b"X" if fake.corrupt else data)
                if self.headers.get("Authorization") != "Bearer anon":
                    challenge = ("Basic realm=\"x\"" if fake.private else
                                 f'Bearer realm="{base}/token",service="fake",scope="repository:{PATH}:pull"')
                    return self.reply(401, b"", {"WWW-Authenticate": challenge})
                m = re.fullmatch(rf"/v2/{PATH}/(manifests|blobs)/(.+)", self.path)
                if not m:
                    return self.reply(404)
                if m.group(1) == "blobs":
                    return self.reply(307, b"", {"Location": f"{base}/storage/{m.group(2)}"})
                body = fake.tags.get(m.group(2))
                return self.reply(200, body) if body else self.reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.repo = f"http://127.0.0.1:{self.server.server_port}/{PATH}"

    def blob(self, data):
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return digest

    def publish(self, builds):
        catalog = json.dumps({"board": "tf4060r5", "builds": builds}).encode()
        self.tags["tf4060r5-catalog"] = json.dumps({
            "schemaVersion": 2, "layers": [{"digest": self.blob(catalog), "size": len(catalog)}]}).encode()


class FeedBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.ghcr = FakeGhcr()
        self._repo, feed.REPO = feed.REPO, self.ghcr.repo
        self.builds = [self.build("2026-11-08_dee0001", "dev", 3),
                       self.build("v0.6_2026-11-07_abc0002", "release", 2),
                       self.build("2026-11-06_dee0000", "dev", 1),
                       self.build("v0.5_2026-11-01_abc0001", "release", 0)]
        self.ghcr.publish(self.builds)

    def tearDown(self):
        feed.REPO = self._repo
        self.ghcr.server.shutdown()
        self.ghcr.server.server_close()
        super().tearDown()

    def build(self, name, channel, tag):
        image, jed_ = full_image(tag), jed(tag)
        path = make_zip(self.tmp / f"tf4060r5_{name}.zip",
                        {f"{name}/tf4060r5_ram_flash.bin": image, f"{name}/tf4060r5_bus_prod.jed": jed_})
        data = path.read_bytes()
        return {"name": name, "channel": channel, "published": f"2026-11-0{tag + 1}T10:00:00Z",
                "summary": f"build {tag}", "fpga_sha": hashlib.sha256(image).hexdigest(),
                "cpld_sha": hashlib.sha256(jed_).hexdigest(),
                "zip": {"filename": path.name, "size": len(data), "digest": self.ghcr.blob(data)}}

    def installed(self, build):
        state = config.load_state()
        state["fpga"]["sha"], state["cpld"]["sha"] = build["fpga_sha"], build["cpld_sha"]
        config.save_state(state)
        return state


class FeedTests(FeedBase):
    def test_release_mode_hides_dev_builds(self):
        result = feed.check()
        self.assertIsNone(result["error"])
        view = feed.view(result, config.load_state(), dev=False)
        self.assertTrue(view["available"])
        self.assertEqual(view["latest"]["name"], "v0.6_2026-11-07_abc0002")
        self.assertEqual(view["recent"], [])
        state = self.installed(self.builds[3])
        view = feed.view(result, state, dev=False)
        self.assertEqual(view["installed"]["name"], "v0.5_2026-11-01_abc0001")
        self.assertTrue(view["available"])
        view = feed.view(result, self.installed(self.builds[1]), dev=False)
        self.assertFalse(view["available"])

    def test_developer_mode(self):
        result = feed.check()
        state = self.installed(self.builds[1])
        view = feed.view(result, state, dev=True)
        self.assertTrue(view["available"])
        self.assertEqual(view["latest"]["name"], "2026-11-08_dee0001")
        self.assertEqual(len(view["recent"]), 4)
        # On the dev build, developer mode turned off: no "update" to the older release.
        view = feed.view(result, self.installed(self.builds[0]), dev=False)
        self.assertFalse(view["available"])
        self.assertTrue(view["installed_newer"])

    def test_download_checks_digest_and_skips_token_on_redirect(self):
        feed.check()
        folder = self.tmp / "dl"
        folder.mkdir()
        path = feed.download(self.builds[1], folder)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.builds[1]["zip"]["digest"][7:])
        self.ghcr.corrupt = True
        path.unlink()
        with self.assertRaises(feed.FeedError):
            feed.download(self.builds[1], folder)
        self.assertEqual(list(folder.iterdir()), [])

    def test_errors_keep_last_list(self):
        feed.check()
        feed.REPO = "http://127.0.0.1:9/nothing/here"
        result = feed.check()
        self.assertIn("can't reach", result["error"])
        self.assertEqual(len(result["builds"]), 4)

    def test_private_package_says_so(self):
        self.ghcr.private = True
        self.assertIn("public", feed.check()["error"])

    def test_nothing_published_yet_or_private(self):
        self.ghcr.missing = 403
        self.assertEqual(feed.check()["error"], feed.NOT_PUBLISHED)
        self.ghcr.missing = 401
        self.assertEqual(feed.check()["error"], feed.NOT_PUBLIC)

    def test_versions_from_build_names(self):
        builds = {b["name"]: b for b in feed.check()["builds"]}
        self.assertEqual(builds["v0.6_2026-11-07_abc0002"]["version"], "v0.6")
        self.assertEqual(builds["v0.6_2026-11-07_abc0002"]["date"], "2026-11-07")
        self.assertIsNone(builds["2026-11-08_dee0001"]["version"])
        self.assertEqual(builds["2026-11-08_dee0001"]["short"], "dee0001")
        tagged = dict(self.builds[3], name="odd-name", version="v0.1")      # version given by the publisher
        self.ghcr.publish([tagged])
        self.assertEqual(feed.check()["builds"][0]["version"], "v0.1")

    def test_bad_catalog_entries_dropped(self):
        bad = dict(self.builds[1], zip={"filename": "../x.sh", "digest": "md5:1"})
        self.ghcr.publish([bad, self.builds[3]])
        self.assertEqual([b["name"] for b in feed.check()["builds"]], ["v0.5_2026-11-01_abc0001"])


class FeedWebTests(FeedBase):
    def setUp(self):
        super().setUp()
        from tfpi.web import create_app
        self.client = create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def csrf(self, path):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def post(self, path, **form):
        return self.client.post(path, data={"csrf": self.csrf("/"), **form})

    def test_update_offered_and_fetched_into_review(self):
        self.assertIn("Not checked yet", self.client.get("/firmware").get_data(as_text=True))
        self.assertIn("Not checked for new firmware yet", self.client.get("/").get_data(as_text=True))
        self.post("/firmware/check")
        self.installed(self.builds[3])
        home = self.client.get("/").get_data(as_text=True)                 # the overview says so too
        self.assertIn("<b>Update available:</b> <b>v0.6</b>", home)
        self.assertIn('value="v0.6_2026-11-07_abc0002"', home)
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn("Firmware update available", page)
        self.assertIn("<b>v0.6</b>", page)                                 # the new one, by version
        self.assertIn("Installed: <b>v0.5</b>", page)
        self.assertIn("v0.6_2026-11-07_abc0002", page)
        self.assertNotIn("dee0001", page)
        reply = self.post("/firmware/get", name="v0.6_2026-11-07_abc0002")
        self.assertIn("/review/", reply.headers["Location"])
        review = self.client.get(reply.headers["Location"]).get_data(as_text=True)
        self.assertIn("tf4060r5_v0.6_2026-11-07_abc0002.zip", review)

    def test_dev_builds_need_developer_mode(self):
        self.post("/firmware/check")
        reply = self.post("/firmware/get", name="2026-11-08_dee0001")
        self.assertEqual(reply.headers["Location"], "/firmware")
        self.post("/settings/developer", on="1")
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn("Recent builds", page)
        self.assertIn("2026-11-08_dee0001", page)
        self.assertIn("<b>developer build</b>", page)
        self.assertIn("not a tested release", page)
        self.assertIn("/review/", self.post("/firmware/get", name="2026-11-08_dee0001").headers["Location"])
        self.post("/settings/developer", on="0")
        self.assertNotIn("Recent builds", self.client.get("/firmware").get_data(as_text=True))


class HomeTests(FeedBase):
    """The landing page: one line each for firmware and TF Pi software."""

    def setUp(self):
        super().setUp()
        from tfpi import update
        from tfpi.web import create_app
        self.update = update
        self._check = update.check
        self.software = {"time": 1, "error": None, "available": False}

        def fake_check():
            config.write_json(update.check_file(), dict(self.software))
            return dict(self.software)
        update.check = fake_check
        self.client = create_app().test_client()
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', self.client.get("/setup").get_data(as_text=True)).group(1)
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def tearDown(self):
        self.update.check = self._check
        super().tearDown()

    def csrf(self):
        return re.search(r'name="csrf" value="([0-9a-f]+)"', self.client.get("/").get_data(as_text=True)).group(1)

    def test_tabs_logo_and_favicon(self):
        page = self.client.get("/").get_data(as_text=True)
        for href in ("/firmware", "/updates", "/files", "/diag"):
            self.assertIn(f'href="{href}"', page)
        self.assertIn('class="tab active" href="/"', page)
        self.assertIn('class="tab active" href="/updates"', self.client.get("/updates").get_data(as_text=True))
        self.assertIn("discord.gg/4B8743Fm4w", page)
        self.assertEqual(self.client.get("/static/favicon.png").status_code, 200)
        self.client.post("/logout", data={"csrf": self.csrf()})
        self.assertEqual(self.client.get("/static/logo.png").status_code, 200)    # login page has it too

    def test_check_all_reports_both(self):
        reply = self.client.post("/check-all", data={"csrf": self.csrf()}, follow_redirects=True)
        page = reply.get_data(as_text=True)
        self.assertIn("Found new firmware.", page)
        self.assertIn("Up to date.", page)                              # TF Pi software
        self.assertIn("<b>Update available:</b> <b>v0.6</b>", page)
        self.software = {"time": 1, "error": None, "available": True, "installed": "a" * 40,
                         "latest": "b" * 40, "changes": ["Firmware tab"], "rebuild": [],
                         "a314d_interrupted": False}
        self.installed(self.builds[1])
        page = self.client.post("/check-all", data={"csrf": self.csrf()}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Found a TF Pi software update.", page)
        self.assertIn("aaaaaaa → bbbbbbb", page)
        self.assertIn("a314d keeps running.", page)
        self.assertIn('href="/updates">Review and update', page)
        updates = self.client.get("/updates").get_data(as_text=True)
        self.assertIn("Update now", updates)
        self.assertNotIn("Published firmware", updates)

    def test_update_actions_return_to_updates_tab(self):
        reply = self.client.post("/update/check", data={"csrf": self.csrf()})
        self.assertEqual(reply.headers["Location"], "/updates")

    def test_firmware_tab_check_first_and_developer_switch(self):
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertLess(page.index("Check for new firmware"), page.index("Installed firmware"))
        self.assertIn('type="checkbox" name="on" value="1"  onchange', page)       # off
        self.client.post("/settings/developer", data={"csrf": self.csrf(), "on": "1"})
        self.assertIn('value="1" checked onchange', self.client.get("/firmware").get_data(as_text=True))
        self.client.post("/settings/developer", data={"csrf": self.csrf()})          # unticked: no field
        self.assertIn('value="1"  onchange', self.client.get("/firmware").get_data(as_text=True))
