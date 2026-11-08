import io
import re
import time

from helpers import Sandbox, full_image, jed, make_zip
from tfpi import config


class WebTests(Sandbox):
    def setUp(self):
        super().setUp()
        from tfpi.web import create_app
        self.client = create_app().test_client()

    def csrf(self, path="/login"):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def login(self):
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def upload(self, files, name="fw.zip"):
        data = make_zip(self.tmp / name, files).read_bytes()
        return self.client.post("/upload", data={"csrf": self.csrf("/"),
                                "firmware": (io.BytesIO(data), name)},
                                content_type="multipart/form-data")

    def test_requires_setup_and_login(self):
        self.assertIn("/setup", self.client.get("/").headers["Location"])
        self.login()
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/logout", data={"csrf": self.csrf("/")})
        self.assertIn("/login", self.client.get("/").headers["Location"])
        bad = self.client.post("/login", data={"csrf": self.csrf(), "password": "nope"})
        self.assertIn("Wrong password", bad.get_data(as_text=True))

    def test_post_without_csrf_rejected(self):
        self.login()
        self.assertEqual(self.client.post("/upload", data={}).status_code, 400)

    def test_wrong_board_rejected_on_review(self):
        self.login()
        review = self.upload({"tf4060r4_ram.bin": full_image()})
        page = self.client.get(review.headers["Location"], follow_redirects=True).get_data(as_text=True)
        self.assertIn("rejected", page)

    def test_upload_review_flash(self):
        self.login()
        review_url = self.upload({"tf4060r5_ram.bin": full_image(), "tf4060r5_bus_top.jed": jed(),
                                  "seed.txt": "7"}).headers["Location"]
        page = self.client.get(review_url).get_data(as_text=True)
        self.assertIn("will be flashed", page)
        self.assertIn("XC95288XL", page)

        token = review_url.rsplit("/", 1)[1]
        started = self.client.post(f"/flash/{token}", data={"csrf": self.csrf("/")})
        job = started.headers["Location"].rsplit("/", 1)[1]
        log, offset, status = "", 0, None
        for _ in range(100):
            reply = self.client.get(f"/job/{job}/log?offset={offset}").get_json()
            log += reply["text"]
            offset = reply["offset"]
            status = reply["status"]
            if status == "done":
                break
            time.sleep(0.2)
        self.assertEqual(status, "done", log)
        self.assertTrue(reply["result"]["ok"], log)
        self.assertIn("CPLD: programmed and verified", log)

        index = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn("fw.zip", index)
        self.assertIn("(installed)", index)
        # Rollback/review of the stored zip now says nothing to flash.
        again = self.client.post("/stored/fw.zip", data={"csrf": self.csrf("/")})
        page = self.client.get(again.headers["Location"]).get_data(as_text=True)
        self.assertEqual(page.count("already installed"), 2)
