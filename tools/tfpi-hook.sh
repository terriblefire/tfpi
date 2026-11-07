#!/usr/bin/env bash
# Prepare a freshly written Raspberry Pi OS card for TF Pi setup.
#   tools/tfpi-hook.sh /Volumes/bootfs        (macOS)
#   tools/tfpi-hook.sh /media/$USER/bootfs    (Linux)
#
# Copies vendor-data (Trixie / cloud-init images). For Bookworm images, where
# Imager writes firstrun.sh instead, it also adds one line to firstrun.sh that
# runs the same setup.
set -euo pipefail
boot=${1:?usage: $0 <path to mounted bootfs>}
here=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$boot/config.txt" ] || { echo "$boot does not look like a Pi boot partition" >&2; exit 1; }

cp "$here/bootfs/vendor-data" "$boot/vendor-data"
# A browser download saved as vendor-data.txt is ignored by cloud-init; drop it.
rm -f "$boot/vendor-data.txt" "$boot/._vendor-data.txt"
echo "copied vendor-data"

if [ -f "$boot/firstrun.sh" ]; then
	cp "$here/bootfs/vendor-data" "$boot/tfpi-firstboot.sh"
	if grep -q tfpi-firstboot "$boot/firstrun.sh"; then
		echo "firstrun.sh already patched"
	else
		ADD='for d in /boot/firmware /boot; do [ -f $d/tfpi-firstboot.sh ] && { sh $d/tfpi-firstboot.sh; break; }; done' \
		awk '!done && /rm -f .*firstrun\.sh/ { print ENVIRON["ADD"]; done = 1 } { print }
			END { if (!done) exit 1 }' "$boot/firstrun.sh" > "$boot/firstrun.sh.tfpi" \
			|| { rm -f "$boot/firstrun.sh.tfpi"; echo "could not find where to hook firstrun.sh" >&2; exit 1; }
		mv "$boot/firstrun.sh.tfpi" "$boot/firstrun.sh"
		echo "patched firstrun.sh (Bookworm)"
	fi
fi
sync
echo "Done. Eject the card and boot the Pi."
