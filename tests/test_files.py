import io
import json
import os
import re

from helpers import Sandbox
from tfpi import files


class FilesTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "a314shared"
        self.root.mkdir()
        conf = self.tmp / "a314fs.conf"
        conf.write_text(json.dumps({"devices": {"PI0": {"volume": "PiDisk", "path": str(self.root)}}}))
        self._conf, files.A314FS_CONF = files.A314FS_CONF, conf
        from tfpi.web import create_app
        self.client = create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})
        self.token = self.csrf("/files")

    def tearDown(self):
        files.A314FS_CONF = self._conf
        super().tearDown()

    def csrf(self, path):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def put(self, name, data, folder="", overwrite=False, csrf=True):
        query = {"dir": folder, "name": name}
        if overwrite:
            query["overwrite"] = "1"
        headers = {"X-CSRF": self.token} if csrf else {}
        return self.client.put("/files/upload", query_string=query, data=data, headers=headers,
                               content_type="application/octet-stream")

    def post(self, action, **form):
        return self.client.post(f"/files/{action}", data={"csrf": self.token, **form}, follow_redirects=True)

    def test_needs_login(self):
        self.client.post("/logout", data={"csrf": self.token})
        self.token = self.csrf("/login")
        self.assertIn("/login", self.client.get("/files").headers["Location"])
        self.assertIn("/login", self.put("x", b"1").headers["Location"])
        self.assertFalse((self.root / "x").exists())

    def test_put_needs_csrf_header(self):
        self.assertEqual(self.put("x", b"1", csrf=False).status_code, 400)
        self.assertFalse((self.root / "x").exists())

    def test_upload_list_download(self):
        reply = self.put("Game Disk 1.adf", b"\0" * 901120)
        self.assertEqual(reply.get_json(), {"saved": "Game Disk 1.adf"})
        self.assertEqual(self.put("Tools/C/Dir", b"hunk").status_code, 200)   # dropped folder
        self.assertEqual((self.root / "Tools" / "C" / "Dir").read_bytes(), b"hunk")
        self.assertEqual(self.put("Lha", b"x", folder="tools/c").get_json()["saved"], "Tools/C/Lha")
        page = self.client.get("/files").get_data(as_text=True)
        self.assertIn("PiDisk:", page)
        self.assertIn("Game Disk 1.adf", page)
        self.assertIn("880.0 KB", page)
        self.assertNotIn(files.PART, page)
        # Links on the page work as they are.
        link = re.search(r'href="(/files/download\?path=Game[^"]*)"', page).group(1).replace("&amp;", "&")
        self.assertEqual(len(self.client.get(link).data), 901120)
        self.assertIn('name="folder" value=""', page)
        self.assertIn('/files?path=Tools"', page)
        got = self.client.get("/files/download", query_string={"path": "tools/c/dir"})
        self.assertEqual(got.data, b"hunk")

    def test_overwrite_needs_confirmation_and_keeps_case_insensitive_name(self):
        self.put("ReadMe", b"old")
        (self.root / "ReadMe:a314").write_text("p0\nchello\n")
        reply = self.put("README", b"new")
        self.assertEqual(reply.status_code, 409)
        self.assertTrue(reply.get_json()["exists"])
        self.assertEqual(self.put("README", b"new", overwrite=True).status_code, 200)
        self.assertEqual(sorted(os.listdir(self.root)), ["README", "README:a314"])
        self.assertEqual((self.root / "README").read_bytes(), b"new")

    def test_bad_names_and_junk(self):
        for name in ("../escape", "a:b", "café😀", ".DS_Store", "._Game.adf", "x:a314"):
            self.assertEqual(self.put(name, b"1").status_code, 400, name)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse((self.tmp / "escape").exists())
        self.assertEqual(self.put("café", b"1").status_code, 200)       # Latin-1 is fine

    def test_short_upload_leaves_nothing(self):
        sh = files.share()
        with self.assertRaises(files.FilesError):
            files.save(sh, "", "cut", io.BytesIO(b"abc"), 10)
        self.assertEqual(os.listdir(self.root), [])

    def test_symlink_outside_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret").write_text("no")
        os.symlink(outside, self.root / "link")
        self.assertEqual(self.client.get("/files/download", query_string={"path": "link/secret"}).status_code, 404)
        self.assertEqual(self.put("x", b"1", folder="link").status_code, 400)
        self.assertFalse((outside / "x").exists())
        self.post("delete", path="link")                     # removes the link, not the folder
        self.assertTrue((outside / "secret").exists())
        self.assertFalse(os.path.lexists(self.root / "link"))

    def test_mkdir_rename_delete(self):
        self.post("mkdir", folder="", name="Games")
        self.assertTrue((self.root / "Games").is_dir())
        page = self.post("mkdir", folder="", name="games").get_data(as_text=True)
        self.assertIn("already exists", page)
        self.put("a.adf", b"1", folder="Games")
        (self.root / "Games" / "a.adf:a314").write_text("p2\n")
        self.post("rename", folder="Games", path="Games/a.adf", name="Lemmings.adf")
        self.assertEqual(sorted(os.listdir(self.root / "Games")), ["Lemmings.adf", "Lemmings.adf:a314"])
        page = self.client.get("/files", query_string={"path": "games"}).get_data(as_text=True)
        self.assertIn("Lemmings.adf", page)
        self.assertNotIn("a314<", page)
        self.post("delete", folder="Games", path="Games/Lemmings.adf")
        self.assertEqual(os.listdir(self.root / "Games"), [])
        self.post("delete", folder="", path="Games")
        self.assertEqual(os.listdir(self.root), [])
        self.assertIn("delete the share", self.post("delete", path="").get_data(as_text=True))

    def test_other_posts_still_capped(self):
        reply = self.client.post("/upload", data={"csrf": self.token, "firmware": (io.BytesIO(b"\0" * (9 << 20)), "big.zip")},
                                 content_type="multipart/form-data")
        self.assertEqual(reply.status_code, 302)
        big = self.put("big.hdf", b"\0" * (9 << 20))
        self.assertEqual(big.status_code, 200)

    def test_no_share_configured(self):
        files.A314FS_CONF = self.tmp / "missing.conf"
        page = self.client.get("/files", follow_redirects=True).get_data(as_text=True)
        self.assertIn("share isn", page)
