import io
import re
import time

from helpers import Sandbox, bitstream, full_image, jed, make_zip
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

    def test_ntsc_switch_and_review(self):
        self.login()
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn("I have an NTSC machine", page)
        self.assertNotIn('value="1" checked onchange="this.form.submit()" aria-label="I have an NTSC', page)
        files = {"tf4060r5_ram_flash.bin": full_image(), "tf4060r5_bus_prod.jed": jed(0),
                 "tf4060r5_bus_ntsc.jed": jed(1)}
        page = self.client.get(self.upload(files).headers["Location"]).get_data(as_text=True)
        self.assertIn("tf4060r5_bus_prod.jed</span><br>", page)

        self.client.post("/settings/ntsc", data={"csrf": self.csrf("/"), "on": "1"})
        self.assertTrue(config.ntsc())
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn('value="1" checked onchange="this.form.submit()" aria-label="I have an NTSC', page)
        page = self.client.get(self.upload(files, "fw2.zip").headers["Location"]).get_data(as_text=True)
        self.assertIn("tf4060r5_bus_ntsc.jed</span> <b>NTSC</b>", page)
        self.client.post("/settings/ntsc", data={"csrf": self.csrf("/")})          # unticked: no field
        self.assertFalse(config.ntsc())

    def test_ntsc_switch_on_review_page(self):
        self.login()
        files = {"tf4060r5_ram_flash.bin": full_image(), "tf4060r5_bus_prod.jed": jed(0),
                 "tf4060r5_bus_ntsc.jed": jed(1)}
        review = self.upload(files).headers["Location"]
        page = self.client.get(review).get_data(as_text=True)
        self.assertIn("I have an NTSC machine", page)
        self.assertIn(f'name="next" value="{review}"', page)
        self.assertIn("tf4060r5_bus_prod.jed</span><br>", page)
        # the switch on the review page comes back to it, now with the NTSC CPLD
        r = self.client.post("/settings/ntsc", data={"csrf": self.csrf("/"), "on": "1", "next": review})
        self.assertTrue(r.headers["Location"].endswith(review))
        page = self.client.get(review).get_data(as_text=True)
        self.assertIn("tf4060r5_bus_ntsc.jed</span> <b>NTSC</b>", page)
        # anything but a review page goes to the Firmware page
        r = self.client.post("/settings/ntsc", data={"csrf": self.csrf("/"), "next": "//evil.example/"})
        self.assertTrue(r.headers["Location"].endswith("/firmware"))

    def test_prune_jobs_keeps_the_newest_by_time(self):
        # 25 older update- logs, then a flash job (named by date, which sorts first)
        from tfpi.web import prune_jobs
        import os
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(25):
            log = config.JOB_DIR / f"update-20261101-0000{i:02d}-aaaaaa.log"
            log.write_text("old")
            os.utime(log, (1000 + i, 1000 + i))
        flash_log = config.JOB_DIR / "20261122-114922-252913.log"
        flash_log.write_text("new")
        prune_jobs()
        logs = sorted(config.JOB_DIR.glob("*.log"))
        self.assertEqual(len(logs), 20)
        self.assertTrue(flash_log.exists())
        self.assertFalse((config.JOB_DIR / "update-20261101-000000-aaaaaa.log").exists())

    def test_wrong_board_rejected_on_review(self):
        self.login()
        review = self.upload({"tf4060r4_ram.bin": full_image()})
        page = self.client.get(review.headers["Location"], follow_redirects=True).get_data(as_text=True)
        self.assertIn("rejected", page)

    def test_raw_bitstream_without_spirom_warns_on_review(self):
        self.login()
        review_url = self.upload({"tf4060r5_ram.bin": bitstream(), "tf4060r5_bus.jed": jed()}).headers["Location"]
        page = self.client.get(review_url).get_data(as_text=True)
        self.assertNotIn("rejected", page)
        self.assertIn("there is no spirom.img", page)
        self.assertIn("will be flashed", page)
        self.assertIn("Flash now", page)

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
