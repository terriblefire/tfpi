# TF Pi

Sets up a Raspberry Pi to drive a TerribleFire TF4060: A314 daemon (TF build),
`fomu-flash` for the RAM FPGA's SPI flash, `xc3sprog` for the BUS CPLD, and a
small web page to deploy firmware releases.

## Set up a Pi

1. In **Raspberry Pi Imager**, write **Raspberry Pi OS Lite** to the SD card.
   In the OS customisation settings, set a hostname, a user and password,
   Wi-Fi if needed, and enable SSH.
2. Take the card out and put it back in. A drive called `bootfs` appears.
3. Download [`vendor-data`](bootfs/vendor-data) and copy it onto `bootfs`
   (next to `config.txt` and `user-data`).
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
- Only one flash runs at a time.

The page is meant for your home network only. Don't forward it to the internet.

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

`fomu-flash` drives the BCM GPIO block directly, so a Pi 5 is not supported.

## Development

```
python3 -m venv .venv && .venv/bin/pip install flask
.venv/bin/python -m unittest discover -s tests
```

The tests use stub `fomu-flash`, `xc3sprog`, `pinctrl` and `systemctl`. If
`../tf4060/boards/tf4060r5` exists, the real release zips are checked too.
