# TF Pi

Sets up a Raspberry Pi to drive a TerribleFire TF4060: A314 daemon (TF build),
`fomu-flash` for the RAM FPGA's SPI flash, `xc3sprog` for the BUS CPLD, and a
small web page to deploy firmware releases.

Support: [Discord](https://discord.gg/4B8743Fm4w)

## Set up a Pi

1. In **Raspberry Pi Imager**, write **Raspberry Pi OS Lite** to the SD card.
   In the OS customisation settings, set a hostname, a user and password,
   Wi-Fi if needed, and enable SSH.
2. Take the card out and put it back in. A drive called `bootfs` appears.
3. Download [`vendor-data`](bootfs/vendor-data) and copy it onto `bootfs`
   (next to `config.txt` and `user-data`).
   - The name must be exactly `vendor-data`, with no extension. Browsers often
     save it as `vendor-data.txt`, and Finder and Explorer hide the `.txt`;
     cloud-init then ignores it and nothing is installed. Check with
     `ls /Volumes/bootfs` (macOS) or turn on file name extensions (Windows),
     and rename it if needed.
   - Bookworm images (Imager writes a `firstrun.sh` instead of `user-data`):
     run `tools/tfpi-hook.sh /Volumes/bootfs` (macOS) or
     `tools/tfpi-hook.sh /media/$USER/bootfs` (Linux) instead.
4. Eject the card, put it in the Pi on the TF4060, power up.

The first boot downloads and builds everything (allow 20–30 minutes on a
Pi 3/4), then the Pi reboots. Progress is in `/var/log/tfpi-install.log`.

Already have a running Pi? Run:

```
curl -fsSL https://terriblefire.github.io/tfpi/install.sh | sudo bash
```

## Deploy firmware

Open `http://<hostname>.local/`. The first visit asks you to choose a password.

1. Upload a release zip (for example `tf4060r5_2026_04_23_0aac415_12656.zip`).
2. The review page shows which chips will be flashed. A chip whose image is
   already installed is skipped.
3. Press **Flash now** and watch the log. The Amiga must be powered.

The page keeps the last three releases for rollback, a history of flashes, and
how many times each chip has been programmed.

### Published firmware

The Pi checks `ghcr.io/terriblefire/tf4060-firmware` for new builds (every 12
hours, or with **Check for firmware**). When a newer release is published, the
main page shows **Firmware update available**; **Download and review** fetches
it into the normal review page, and nothing is flashed until you confirm.

Releases are the tagged builds and show their tag (e.g. **v0.1**).
**Developer mode** (switch on the main page) also offers developer builds (the
untagged `vLATEST` builds, not tested releases) and lists the recent builds so
you can pick an older one.

The installed build is recognised by its FPGA and CPLD images, so it doesn't
matter what the zip was called or how it got onto the Pi.

Builds are published by hand from the tf4060v2 repo (Gitea Actions, the
`publish` workflow, `scripts/ghcr_publish.py`): pick a tag to publish a release,
or `vLATEST` to publish a developer build. The build published last is the one
offered, so publishing an older release again rolls the Pis back to it.
To check what's published from a Pi: `python3 -m tfpi.feed`
(in `/opt/tfpi/lib`).

### Release zip contents

| File | Needed | Notes |
|---|---|---|
| `<board>_ram_flash.bin` or `<board>_ram.bin` | yes | Full SPI flash image, or a raw iCE40 bitstream |
| `*.jed` | no | BUS CPLD. With several, `_bus_prod`, then `_bus_top`, then `_bus` is used |
| `seed.txt` | no | Shown on the page (else "seed N" from `README.md`) |
| `spirom.img` | raw bitstreams only | SSBL/ROMFS combined at 0x21000; can also be placed at `/etc/tfpi/spirom.img` |

Files may be inside a folder in the zip. Zips for another board (the prefix before `_ram`) are refused. The board is
set in `/etc/tfpi/tfpi.conf` (`BOARD=tf4060r5`).

### Safety rules built in

- The SPI flash is identified by its JEDEC ID first; an image bigger than the chip is refused.
- The FPGA flash is read back and compared before the FPGA is reset; then CDONE
  (GPIO 23) must go high.
- The CPLD is programmed **once** per flash, with verify. If erase or verify
  fails, the CPLD is locked out on this Pi: a CPLD that fails verify is failing,
  and more erase cycles can destroy it. Check or replace the chip, then run
  `sudo tfpi-flash clear-cpld-fault`.
- Every flash ends by resetting the FPGA (`fomu-flash -r`, then CDONE), also
  after a CPLD-only flash or a failed one, so the FPGA is never left held in reset.
- Only one flash runs at a time.

The page is meant for your home network only. Don't forward it to the internet.

## Diagnostics

The **Diagnostics** tab captures how the board and the Amiga behave, for us to
look at: a **board test** (experimental: it can report spurious faults, so its
findings are not an indication of a real fault; CPLD JTAG chain, pin snapshot, clocks, CPLD↔FPGA
lines, SDRAM), a **timing capture** (bus timing histograms and waveforms while
the 68060 exercises the Amiga, over the chosen CPU clock presets), or both
(**Run all**), then it always restores and checks the machine. Nothing is
judged good or bad on the Pi: at the end, download the zip and send it to us.
The one real check is the restore: **Restored: OK**, or in red what differs and
what to do.

- Nothing is written to the FPGA flash or the CPLD: the diagnostic images are loaded
  into the FPGA's SRAM, and a power cycle always brings the normal firmware back.
- The tests reset the Amiga, and the first step (stopping a314d and reading the
  FPGA flash with the FPGA held in reset) crashes it. Save your work first, and
  don't power off during a test.
- Every run records which firmware is really on the board, read back from the
  chips (read only), not from what this Pi last flashed: the RAM FPGA flash
  image (SPI ROM build stamp, bitstream and image checksums) and the BUS CPLD
  fuse map (read with xc3sprog's read action while the Amiga is held in reset).
  Both are matched against the releases this Pi keeps, and the zip carries
  `firmware/fpga_bitstream.bin` and `firmware/cpld_readback.jed`.
  `tfdiag fwid --jed FILE --image FILE` gives the same ids for any release.
- The timing capture also lists the expansion boards: before Kickstart can run,
  the exerciser walks the Zorro autoconfig chain (Zorro III, then Zorro II),
  records each board's manufacturer, product, serial and size, and shuts it up
  (or, if it can't be shut up, configures it) so the next one shows. The
  boards go back to normal at the reset that ends the test.
- The timing capture only runs on a BUS CPLD design it was built for: the CPLD fuse
  map read back at the start is checked against `cpld_fuses` in
  `diag_manifest.json` (the usercode is the same on every build). On an older
  design only the board test runs, and the page says to update the firmware
  first. Development boards with test CPLDs can set `"any_cpld": true` in
  `/etc/tfpi/diag.json` (or `TFDIAG_ANY_CPLD=1`).
- If the exerciser does not start, each start attempt still records the 68060's
  bus activity after reset release (`timing/waves/wave_start_a*.vcd` and the
  Amiga-side cycle counts), to show how far the boot got.
- The `tfdiag` tool (`tfdiag/`) and the diagnostic images (`tfpi/diag_images/`,
  with `diag_manifest.json`) are part of tfpi and install with it.
- One job at a time: no flash or update can start during a test, and no test
  during a flash or update.
- The result page lists what is in the download (`tf4060-diag-<serial>-<date>.zip`,
  with IP addresses and host names removed) before you download it. You can add
  a Discord/forum handle and a note on what's wrong; they go only into the zip,
  as `contact.txt`.
- If the power goes during a test, `tfpi-diag-recover.service` restores and
  checks the board at the next boot (before a314d and the page start), and the
  page shows the outcome. Partial results stay downloadable.
- Command line: `sudo PYTHONPATH=/opt/tfpi/lib python3 -m tfdiag --help`.

## Customise the SPI ROM (SSBL and ROMFS)

Upload an **SDK zip** (`tf4060r5_..._SDK.zip`) instead of a release and the page opens the
SPI ROM editor. A full-image release zip has a **Customise SPI ROM** button on its review page.

- **Resident modules**: what the SSBL loads at boot (ehide.device, NCR scsi.device,
  spiromfs, ...), with name, version and size. Add, remove or reorder them. An added
  module needs a ROMTag; files without one (plain programs, blabber.driver) belong in the
  ROMFS. Changing modules relinks the SSBL with vasm/vlink, which needs SDKs that carry
  the SSBL objects (`ssbl/*.o` and `ssbl/toc.i`). Before the first relink the Pi rebuilds
  the unchanged SSBL and checks it matches the SDK's byte for byte.
- **ROMFS** (TF4060:): every file with its size. Add files to any folder (executables keep
  their execute flag; same name replaces), remove files or folders.
- A meter shows the SPI ROM size against the limit: flash size (`SPI_FLASH_BYTES` in
  `/etc/tfpi/tfpi.conf`, default 2 MB) minus the 0x21000 bitstream area. Changes that
  don't fit are refused.
- **Build firmware** makes a normal firmware zip (flash image + README of the changes),
  with the bitstream built from the SDK by icebram/icepack, and opens the review page.

## Files on PI0: (the a314fs share)

The **Files** tab shows the folder the Amiga mounts as `PI0:` (`~/a314shared`,
from `/etc/opt/a314/a314fs.conf`), behind the same password. Drop files or
whole folders on the page to upload; click a file to download it; rename,
delete and make folders from the list.

- Uploads go to a hidden temporary file and appear only once complete and
  synced to the card, so the Amiga never sees half a file and a power cut
  can't leave a truncated one.
- Names work like on the Amiga: case doesn't matter (uploading `README` over
  `ReadMe` asks to replace it), `:` and `/` aren't allowed, and characters
  outside Latin-1 are refused. Names longer than 30 characters are allowed
  but marked, since some Amiga programs can't open them.
- Finder/Explorer clutter (`.DS_Store`, `._*`, `Thumbs.db`) is skipped.
- a314fs keeps protection bits and comments in `<name>:a314` files; these are
  hidden here and follow their file when it is renamed or deleted.

## Updating the Pi software

The page checks GitHub for a newer tfpi about twice a day (or on **Check for
updates**) and, if there is one, lists the new commits and an **Update now**
button. That runs `tfpi-update` with a live log, in its own systemd unit so
the page restarting halfway doesn't stop it.

Only parts whose pinned commit changed in `sources.conf` are rebuilt. a314d is
stopped only when the a314 pin changes; the page says so before you press the
button (the Amiga loses PI0: and the other a314 services for a few minutes),
and also warns if the a314d on the Pi isn't the one the installer built. No
flash can start while an update runs. `sudo TFPI_REBUILD=1 tfpi-update`
rebuilds everything.

## Command line

```
sudo tfpi-flash status
sudo tfpi-flash inspect release.zip
sudo tfpi-flash flash release.zip [--force-fpga] [--force-cpld]
sudo tfpi-flash clear-cpld-fault
sudo tfpi-update          # rebuild at the commits pinned in sources.conf
```

## What gets installed

Pinned in [`sources.conf`](sources.conf):

- `fomu-flash` from terriblefire/fomu-flash → `/usr/local/bin/fomu-flash`
- `xc3sprog` from arkadiuszmakarenko/xc3sprog (cable `gpiod_creator`) → `/usr/local/bin/xc3sprog`
- a314 from erique/a314 branch `tf4060`, installed with its own `install-pi.sh tf`
  (a314d, SPI overlay, config.txt/cmdline.txt changes, a314 Python services)
- vasm 2.0d and vlink 0.18a built from source (SHA-256 pinned), `fpga-icestorm` from apt:
  used by the SPI ROM editor
- this repo → `/opt/tfpi`, web page as `tfpi-web.service` on port 80
- `tfdiag` → `/opt/tfpi/lib/tfdiag`, `openocd` and `python3-spidev` from apt for the
  Diagnostics tab, and `tfpi-diag-recover.service` (power-loss recovery, runs only
  after an interrupted test)

`fomu-flash` drives the BCM GPIO block directly, so a Pi 5 is not supported.

## Development

```
python3 -m venv .venv && .venv/bin/pip install flask
.venv/bin/python -m unittest discover -s tests
```

This also runs tfdiag's own tests (`tfdiag/tests`, a fake board).

The tests use stub `fomu-flash`, `xc3sprog`, `pinctrl`, `systemctl` and `tfdiag`. If
`../tf4060/boards/tf4060r5` exists, the real release zips are checked too.
