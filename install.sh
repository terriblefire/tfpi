#!/usr/bin/env bash
# TF Pi installer: fomu-flash, xc3sprog (gpiod_creator), a314d (TF build) and
# the firmware web page. Safe to run again; `sudo tfpi-update` does that.
#
#   curl -fsSL https://terriblefire.github.io/tfpi/install.sh | sudo bash
#
# Environment overrides: TFPI_REPO, TFPI_REF, TFPI_USER, TFPI_NO_REBOOT=1
set -euo pipefail

TFPI_REPO=${TFPI_REPO:-https://github.com/terriblefire/tfpi}
TFPI_REF=${TFPI_REF:-main}
PREFIX=/opt/tfpi
SRC=$PREFIX/src
LOG=/var/log/tfpi-install.log

if [ "$(id -u)" != 0 ]; then
	echo "Run as root: curl -fsSL https://terriblefire.github.io/tfpi/install.sh | sudo bash" >&2
	exit 1
fi

mkdir -p "$SRC" /var/lib/tfpi /etc/tfpi
if [ -z "${TFPI_LOGGING:-}" ]; then
	export TFPI_LOGGING=1
	exec > >(tee -a "$LOG") 2>&1
fi
echo "=== tfpi install $(date -Is)"

export DEBIAN_FRONTEND=noninteractive
# The a314 checkout belongs to the Pi user; let root's git work in it.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'

# --- Stage 1: get the tfpi repo and re-run the installer from it -----------
self_dir=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
	self_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi
if [ "${TFPI_STAGE:-1}" = 1 ] || [ ! -f "$self_dir/sources.conf" ]; then
	command -v git >/dev/null || { apt-get update; apt-get install -y git ca-certificates; }
	if [ -d "$SRC/tfpi/.git" ]; then
		git -C "$SRC/tfpi" fetch --quiet origin
	else
		rm -rf "$SRC/tfpi"
		git clone --quiet "$TFPI_REPO" "$SRC/tfpi"
	fi
	git -C "$SRC/tfpi" checkout --quiet --force --detach "origin/$TFPI_REF" 2>/dev/null \
		|| git -C "$SRC/tfpi" checkout --quiet --force --detach "$TFPI_REF"
	echo "tfpi at $(git -C "$SRC/tfpi" rev-parse --short HEAD)"
	TFPI_STAGE=2 exec bash "$SRC/tfpi/install.sh" "$@"
fi

cd "$self_dir"
# shellcheck source=sources.conf
. ./sources.conf

# --- Who is the Pi user (the one Raspberry Pi Imager created) ---------------
A314_USER=${TFPI_USER:-$(getent passwd 1000 | cut -d: -f1)}
if [ -z "$A314_USER" ]; then
	echo "No user with uid 1000. Create one in Raspberry Pi Imager (OS customisation) and re-run." >&2
	exit 1
fi
echo "Pi user: $A314_USER"

# --- Packages ---------------------------------------------------------------
apt-get update
PKGS="git ca-certificates curl build-essential cmake pkg-config
      libusb-dev libftdi1-dev libgpiod-dev
      device-tree-compiler python3-dev python3-pip python3-virtualenv ifupdown iptables
      python3-flask python3-waitress unzip fpga-icestorm"
for optional in libftdi-dev python3-distutils; do
	if apt-cache show "$optional" >/dev/null 2>&1; then PKGS="$PKGS $optional"; fi
done
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS

# fetch NAME REPO REF [OWNER]: clone/update $SRC/NAME and check out REF
fetch() {
	local dir=$SRC/$1
	if [ -d "$dir/.git" ]; then
		git -C "$dir" remote set-url origin "$2"
		git -C "$dir" fetch --quiet --tags origin
	else
		rm -rf "$dir"
		git clone --quiet "$2" "$dir"
	fi
	git -C "$dir" checkout --quiet --force --detach "$3"
	git -C "$dir" clean -fdxq
	[ -z "${4:-}" ] || chown -R "$4": "$dir"
	echo "$1 at $(git -C "$dir" rev-parse --short HEAD)"
}

# --- fomu-flash -------------------------------------------------------------
fetch fomu-flash "$FOMU_FLASH_REPO" "$FOMU_FLASH_REF"
make -C "$SRC/fomu-flash" -j"$(nproc)"
install -m755 "$SRC/fomu-flash/fomu-flash" /usr/local/bin/fomu-flash

# --- xc3sprog (gpiod_creator cable) ----------------------------------------
gpiod_major=$(pkg-config --modversion libgpiod | cut -d. -f1)
if [ "$gpiod_major" -ge 2 ]; then xc3_ref=$XC3SPROG_REF; else xc3_ref=$XC3SPROG_REF_GPIOD1; fi
echo "libgpiod $(pkg-config --modversion libgpiod): xc3sprog ${xc3_ref:0:7}"
rm -rf "$SRC/xc3sprog/build"
fetch xc3sprog "$XC3SPROG_REPO" "$xc3_ref"
cmake -S "$SRC/xc3sprog" -B "$SRC/xc3sprog/build" -DUSE_FTD2XX=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$SRC/xc3sprog/build" -j"$(nproc)"
cmake --install "$SRC/xc3sprog/build"

# --- vasm + vlink (SSBL relink for the SPI ROM editor) -----------------------
# tarball NAME URL SHA256 -> $SRC/NAME (checked, unpacked)
tarball() {
	local file=$SRC/dl/$(basename "$2")
	mkdir -p "$SRC/dl"
	[ -f "$file" ] && echo "$3  $file" | sha256sum -c --quiet 2>/dev/null || curl -fsSL -o "$file" "$2"
	echo "$3  $file" | sha256sum -c --quiet
	rm -rf "$SRC/$1" && mkdir -p "$SRC/$1"
	tar -xzf "$file" -C "$SRC/$1" --strip-components=1
}
if [ "$(cat /usr/local/share/tfpi/vasm.url 2>/dev/null)" != "$VASM_URL" ] || ! command -v vasmm68k_mot >/dev/null; then
	tarball vasm "$VASM_URL" "$VASM_SHA256"
	make -C "$SRC/vasm" CPU=m68k SYNTAX=mot -j"$(nproc)"
	install -m755 "$SRC/vasm/vasmm68k_mot" /usr/local/bin/vasmm68k_mot
	install -d /usr/local/share/tfpi && echo "$VASM_URL" > /usr/local/share/tfpi/vasm.url
fi
if [ "$(cat /usr/local/share/tfpi/vlink.url 2>/dev/null)" != "$VLINK_URL" ] || ! command -v vlink >/dev/null; then
	tarball vlink "$VLINK_URL" "$VLINK_SHA256"
	mkdir -p "$SRC/vlink/objects"
	make -C "$SRC/vlink" -j"$(nproc)"
	install -m755 "$SRC/vlink/vlink" /usr/local/bin/vlink
	install -d /usr/local/share/tfpi && echo "$VLINK_URL" > /usr/local/share/tfpi/vlink.url
fi
vasmm68k_mot -v 2>&1 | head -1
vlink -v 2>&1 | head -1

# --- a314 (TF build) via its own installer ----------------------------------
# install-pi.sh builds as $SUDO_USER, so the checkout belongs to the Pi user.
fetch a314 "$A314_REPO" "$A314_REF" "$A314_USER"
systemctl stop a314d 2>/dev/null || true
(cd "$SRC/a314/Software" && SUDO_USER=$A314_USER bash ./install-pi.sh tf)

# --- tfpi web page + flasher ------------------------------------------------
install -d "$PREFIX/lib"
rm -rf "$PREFIX/lib/tfpi"
cp -r "$self_dir/tfpi" "$PREFIX/lib/tfpi"
find "$PREFIX/lib/tfpi" -name __pycache__ -prune -exec rm -rf {} +
install -m755 "$self_dir/share/setgpio.sh" "$PREFIX/setgpio.sh"
for tool in tfpi-flash tfpi-update; do
	install -m755 "$self_dir/bin/$tool" "/usr/local/bin/$tool"
done
[ -f /etc/tfpi/tfpi.conf ] || echo "BOARD=$BOARD" > /etc/tfpi/tfpi.conf
install -m644 "$self_dir/systemd/tfpi-web.service" /etc/systemd/system/tfpi-web.service

ver() { printf '"%s": {"repo": "%s", "ref": "%s"}' "$1" "$2" "$(git -C "$SRC/$1" rev-parse HEAD)"; }
{
	echo "{"
	ver tfpi "$TFPI_REPO"; echo ","
	ver fomu-flash "$FOMU_FLASH_REPO"; echo ","
	ver xc3sprog "$XC3SPROG_REPO"; echo ","
	ver a314 "$A314_REPO"; echo
	echo "}"
} > "$PREFIX/versions.json"

systemctl daemon-reload
systemctl enable tfpi-web.service
systemctl restart tfpi-web.service

# --- Done -------------------------------------------------------------------
sync    # the Pi may lose power with the Amiga at any time
first_install=0
[ -f /var/lib/tfpi/installed ] || first_install=1
date -Is > /var/lib/tfpi/installed
sync
systemctl disable tfpi-firstboot.service 2>/dev/null || true

host=$(hostname)
echo
echo "=== tfpi install complete"
echo "Firmware page: http://$host.local/  (set a password on the first visit)"
command -v xc3sprog fomu-flash /opt/a314/a314d || true

if [ "$first_install" = 1 ] && [ -z "${TFPI_NO_REBOOT:-}" ]; then
	echo "Rebooting to load the a314 SPI overlay..."
	systemctl reboot
else
	# install-pi.sh leaves a314d stopped and expects a reboot.
	systemctl start a314d || echo "a314d did not start; reboot to load the SPI overlay"
	echo "Reboot if config.txt changed (a314 SPI overlay): sudo reboot"
fi
