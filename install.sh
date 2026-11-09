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
# installable PKG: apt has a version to install. `apt-cache show` is not enough: on
# Trixie python3-distutils is only referred to by other packages, `show` succeeds and
# apt-get install then fails ("has no installation candidate").
installable() {
	cand=$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
	[ -n "$cand" ] && [ "$cand" != "(none)" ]
}
apt-get update
PKGS="git ca-certificates curl build-essential cmake pkg-config
      libusb-dev libftdi1-dev libgpiod-dev
      device-tree-compiler python3-dev python3-pip python3-virtualenv ifupdown iptables
      python3-flask python3-waitress unzip fpga-icestorm"
for optional in libftdi-dev python3-distutils; do
	if installable "$optional"; then PKGS="$PKGS $optional"; fi
done
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS

# Parts whose pinned commit hasn't changed since the last install are left
# alone (no rebuild; a314d keeps running). TFPI_REBUILD=1 rebuilds everything.
prev() {   # prev NAME FIELD: a value recorded by the last install
	python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],{}).get(sys.argv[3],""))' \
		"$PREFIX/versions.json" "$1" "$2" 2>/dev/null || true
}
needed() { # needed NAME REF BINARY: true if NAME must be (re)built
	[ -n "${TFPI_REBUILD:-}" ] || [ ! -x "$3" ] || [ "$(prev "$1" ref)" != "$2" ] || return 1
}

# fetch NAME REPO REF [OWNER]: clone/update $SRC/NAME and check out REF
fetch() {
	local dir=$SRC/$1
	# A checkout damaged by a power cut (the Pi loses power with the Amiga)
	# is thrown away and cloned again.
	if ! { [ -d "$dir/.git" ] && git -C "$dir" remote set-url origin "$2" \
			&& git -C "$dir" fetch --quiet --tags origin \
			&& git -C "$dir" checkout --quiet --force --detach "$3"; }; then
		rm -rf "$dir"
		git clone --quiet "$2" "$dir"
		git -C "$dir" checkout --quiet --force --detach "$3"
	fi
	git -C "$dir" clean -fdxq
	[ -z "${4:-}" ] || chown -R "$4": "$dir"
	echo "$1 at $(git -C "$dir" rev-parse --short HEAD)"
}

# --- fomu-flash -------------------------------------------------------------
if needed fomu-flash "$FOMU_FLASH_REF" /usr/local/bin/fomu-flash; then
	fetch fomu-flash "$FOMU_FLASH_REPO" "$FOMU_FLASH_REF"
	make -C "$SRC/fomu-flash" -j"$(nproc)"
	install -m755 "$SRC/fomu-flash/fomu-flash" /usr/local/bin/fomu-flash
else
	echo "fomu-flash: unchanged"
fi

# --- xc3sprog (gpiod_creator cable) ----------------------------------------
gpiod_major=$(pkg-config --modversion libgpiod | cut -d. -f1)
if [ "$gpiod_major" -ge 2 ]; then xc3_ref=$XC3SPROG_REF; else xc3_ref=$XC3SPROG_REF_GPIOD1; fi
echo "libgpiod $(pkg-config --modversion libgpiod): xc3sprog ${xc3_ref:0:7}"
if needed xc3sprog "$xc3_ref" /usr/local/bin/xc3sprog; then
	rm -rf "$SRC/xc3sprog/build"
	fetch xc3sprog "$XC3SPROG_REPO" "$xc3_ref"
	cmake -S "$SRC/xc3sprog" -B "$SRC/xc3sprog/build" -DUSE_FTD2XX=OFF -DCMAKE_BUILD_TYPE=Release
	cmake --build "$SRC/xc3sprog/build" -j"$(nproc)"
	cmake --install "$SRC/xc3sprog/build"
else
	echo "xc3sprog: unchanged"
fi

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
# This is the only step that interrupts a314d.
a314_built=0
a314d_sha=$(prev a314 a314d_sha256)
if needed a314 "$A314_REF" /opt/a314/a314d; then
	fetch a314 "$A314_REPO" "$A314_REF" "$A314_USER"
	echo "a314: stopping a314d to rebuild it"
	systemctl stop a314d 2>/dev/null || true
	(cd "$SRC/a314/Software" && SUDO_USER=$A314_USER bash ./install-pi.sh tf)
	a314_built=1
	a314d_sha=$(sha256sum /opt/a314/a314d | cut -d" " -f1)
else
	echo "a314: unchanged, a314d left running"
fi

# --- tfpi web page + flasher ------------------------------------------------
install -d "$PREFIX/lib"
rm -rf "$PREFIX/lib/tfpi" "$PREFIX/lib/tfdiag"
cp -r "$self_dir/tfpi" "$PREFIX/lib/tfpi"
cp -r "$self_dir/tfdiag" "$PREFIX/lib/tfdiag"     # Diagnostics tab (python3 -m tfdiag)
find "$PREFIX/lib/tfpi" "$PREFIX/lib/tfdiag" -name __pycache__ -prune -exec rm -rf {} +
install -m755 "$self_dir/share/setgpio.sh" "$PREFIX/setgpio.sh"
for tool in tfpi-flash tfpi-update; do
	install -m755 "$self_dir/bin/$tool" "/usr/local/bin/$tool"
done
[ -f /etc/tfpi/tfpi.conf ] || echo "BOARD=$BOARD" > /etc/tfpi/tfpi.conf
install -m644 "$self_dir/systemd/tfpi-web.service" /etc/systemd/system/tfpi-web.service
# a314d comes back after it exits (Gitea #59); the unit itself is a314's.
install -d /etc/systemd/system/a314d.service.d
install -m644 "$self_dir/systemd/a314d.service.d/tfpi-restart.conf" /etc/systemd/system/a314d.service.d/tfpi-restart.conf

# ref: what is installed. a314d_sha256: the a314d this installer built (the
# page uses it to warn when a locally built a314d would be replaced).
# If a skipped part's checkout can't be read, keep the ref recorded last time.
ver() {
	local ref
	ref=$(git -C "$SRC/$1" rev-parse HEAD 2>/dev/null) || ref=$(prev "$1" ref)
	printf '"%s": {"repo": "%s", "ref": "%s"%s}' "$1" "$2" "$ref" "${3:-}"
}
{
	echo "{"
	ver tfpi "$TFPI_REPO"; echo ","
	ver fomu-flash "$FOMU_FLASH_REPO"; echo ","
	ver xc3sprog "$XC3SPROG_REPO"; echo ","
	ver a314 "$A314_REPO" ", \"a314d_sha256\": \"$a314d_sha\""; echo
	echo "}"
} > "$PREFIX/versions.json.new"
mv "$PREFIX/versions.json.new" "$PREFIX/versions.json"

systemctl daemon-reload
systemctl enable tfpi-web.service
systemctl restart tfpi-web.service

# --- Diagnostics tab: openocd (CPLD JTAG) + power-loss recovery unit ---------
# tfdiag and the diagnostic images (tfpi/diag_images) were copied with tfpi
# above; /opt/tfpi/diag is tfdiag's default image folder for command-line use.
[ -e "$PREFIX/diag" ] && [ ! -L "$PREFIX/diag" ] || ln -sfn "$PREFIX/lib/tfpi/diag_images" "$PREFIX/diag"
diag_pkgs=openocd
installable python3-spidev && diag_pkgs="$diag_pkgs python3-spidev"
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $diag_pkgs \
	|| echo "WARNING: could not install $diag_pkgs; the Diagnostics board test needs openocd"
install -m644 "$self_dir/systemd/tfpi-diag-recover.service" /etc/systemd/system/tfpi-diag-recover.service
systemctl daemon-reload
systemctl enable tfpi-diag-recover.service

# --- Done -------------------------------------------------------------------
sync    # the Pi may lose power with the Amiga at any time
first_install=0
[ -f /var/lib/tfpi/installed ] || first_install=1
date -Is > /var/lib/tfpi/installed
sync
systemctl disable tfpi-firstboot.service 2>/dev/null || true
systemctl disable --now tfpi-installing.service 2>/dev/null || true
rm -f /etc/systemd/system/tfpi-installing.service /usr/local/lib/tfpi-installing.py

host=$(hostname)
echo
echo "=== tfpi install complete"
echo "Firmware page: http://$host.local/  (set a password on the first visit)"
command -v xc3sprog fomu-flash /opt/a314/a314d || true

if [ "$first_install" = 1 ] && [ -z "${TFPI_NO_REBOOT:-}" ]; then
	echo "Rebooting to load the a314 SPI overlay..."
	systemctl reboot
elif [ "$a314_built" = 1 ]; then
	# install-pi.sh leaves a314d stopped and expects a reboot.
	systemctl start a314d || echo "a314d did not start; reboot to load the SPI overlay"
	echo "Reboot if config.txt changed (a314 SPI overlay): sudo reboot"
fi
