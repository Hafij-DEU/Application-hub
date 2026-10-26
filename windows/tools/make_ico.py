"""Make data/app-hub.ico from src/icons/app-hub.png (PNG-compressed entries, Windows Vista+).

    python tools/make_ico.py        (MSYS2 UCRT64 Python: needs GdkPixbuf to scale)
"""

import struct
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def main():
    src = GdkPixbuf.Pixbuf.new_from_file(str(ROOT / "src" / "icons" / "app-hub.png"))
    images = []
    for size in SIZES:
        scaled = src.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
        ok, data = scaled.save_to_bufferv("png", [], [])
        images.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size          # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    out = ROOT / "data" / "app-hub.ico"
    out.write_bytes(header + entries + blobs)
    print("wrote", out, out.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
