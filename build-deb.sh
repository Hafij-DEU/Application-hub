#!/bin/sh
# Build dist/app-hub_<version>_all.deb
set -eu
cd "$(dirname "$0")"
VERSION=$(python3 -B -c 'import sys; sys.path.insert(0, "src"); import app_hub; print(app_hub.__version__)')
# Stage in /tmp: on NTFS/FAT drives every file is 777, which dpkg-deb rejects.
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
ROOT=$BUILD/root
mkdir -p "$ROOT/DEBIAN" dist
chmod 755 "$ROOT" "$ROOT/DEBIAN"

install -Dm755 data/app-hub "$ROOT/usr/bin/app-hub"
install -Dm755 src/app-hub-helper "$ROOT/usr/lib/app-hub/app-hub-helper"
install -Dm644 src/catalog.json "$ROOT/usr/lib/app-hub/catalog.json"
install -Dm644 src/library-default.json "$ROOT/usr/lib/app-hub/library-default.json"
for f in src/app_hub/*.py; do install -Dm644 "$f" "$ROOT/usr/lib/app-hub/app_hub/$(basename "$f")"; done
for f in src/icons/*.png; do install -Dm644 "$f" "$ROOT/usr/lib/app-hub/icons/$(basename "$f")"; done
install -Dm644 data/io.github.hafij_bge.AppHub.desktop "$ROOT/usr/share/applications/io.github.hafij_bge.AppHub.desktop"
install -Dm644 data/io.github.hafij_bge.AppHub.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/io.github.hafij_bge.AppHub.svg"
install -Dm644 data/io.github.hafij_bge.AppHub.policy "$ROOT/usr/share/polkit-1/actions/io.github.hafij_bge.AppHub.policy"
install -Dm644 README.md "$ROOT/usr/share/doc/app-hub/README.md"
install -Dm644 LICENSE "$ROOT/usr/share/doc/app-hub/copyright"
install -Dm644 THIRD_PARTY_NOTICES.md "$ROOT/usr/share/doc/app-hub/THIRD_PARTY_NOTICES.md"

SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" debian/control > "$ROOT/DEBIAN/control"
install -m755 debian/postrm "$ROOT/DEBIAN/postrm"

dpkg-deb --root-owner-group -Zxz --build "$ROOT" "dist/app-hub_${VERSION}_all.deb"
