#!/usr/bin/env python3
"""Set (or clear) the admin PIN. Entering it in App Hub ▸ Preferences ▸ Admin shows
Add App in My Apps on that computer.

  ./set-admin-pin.py          ask for a new PIN twice and store its hash
  ./set-admin-pin.py --clear  remove the PIN (Add App is then always shown)

Only a salted PBKDF2 hash goes into src/catalog.json; the PIN itself is never
stored. Rebuild (./build-deb.sh) and release so installed copies get it.
"""
import getpass
import hashlib
import json
import os
import secrets
import sys

CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "catalog.json")
ITERATIONS = 600_000


def main():
    with open(CATALOG, encoding="utf-8") as f:
        data = json.load(f)
    if "--clear" in sys.argv[1:]:
        data.pop("admin_pin", None)
        print("Admin PIN removed.")
    else:
        pin = getpass.getpass("New admin PIN (at least 4 characters): ")
        if len(pin) < 4:
            sys.exit("Too short: use at least 4 characters.")
        if getpass.getpass("Repeat the PIN: ") != pin:
            sys.exit("The PINs do not match. Nothing changed.")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITERATIONS).hex()
        data["admin_pin"] = {"salt": salt, "hash": digest, "iterations": ITERATIONS}
        print("Admin PIN set.")
    with open(CATALOG, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
