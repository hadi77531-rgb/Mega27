#!/usr/bin/env python3
"""
============================================================
  make_cookie_env.py - Back4App / Railway safe cookie variables
============================================================

Back4App limits ONE environment variable to 1024 characters, so a full
Netscape cookies.txt does not fit.  This tool gzips + base64-encodes the
file and splits it into numbered variables the bot knows how to join:

    YOUTUBE_COOKIES_B64_1=...
    YOUTUBE_COOKIES_B64_2=...

Usage:
    python make_cookie_env.py cookies.txt
    python make_cookie_env.py cookies.txt --youtube-only
    python make_cookie_env.py cookies.txt --out cookies.env

Then paste every printed line into the Back4App environment page.
============================================================
"""
import argparse
import base64
import gzip
import io
import os
import sys

DEFAULT_CHUNK = 1000  # Back4App caps at 1024; keep headroom

# Cookies that actually authenticate a YouTube session.
# Everything else (PREF, SOCS, device fingerprinting, ...) only adds weight.
AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "LOGIN_INFO", "__Secure-ROLLOUT_TOKEN", "__Secure-YNID",
    "SOCS",
}


def youtube_only(text: str):
    """Drop rows that are not YouTube authentication cookies."""
    header, kept, dropped = [], [], []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            header.append(line)
            continue
        fields = line.split("\t")
        name = fields[5] if len(fields) > 5 else ""
        if name in AUTH_COOKIES:
            kept.append(line)
        else:
            dropped.append(name)
    body = "\n".join(header + kept)
    if not body.endswith("\n"):
        body += "\n"
    return body, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cookies", help="path to a Netscape cookies.txt")
    ap.add_argument("--var", default="YOUTUBE_COOKIES_B64",
                    help="env var prefix (default: YOUTUBE_COOKIES_B64)")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                    help=f"characters per variable (default: {DEFAULT_CHUNK})")
    ap.add_argument("--youtube-only", action="store_true",
                    help="keep only YouTube authentication cookies")
    ap.add_argument("--out", help="also write the lines to this file")
    args = ap.parse_args()

    try:
        with io.open(args.cookies, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"ERROR: cannot read {args.cookies}: {exc}", file=sys.stderr)
        return 1

    if "Netscape HTTP Cookie File" not in text[:400] and ".youtube.com" not in text:
        print("ERROR: that does not look like a Netscape cookies.txt", file=sys.stderr)
        return 1

    dropped = []
    if args.youtube_only:
        text, dropped = youtube_only(text)

    raw_bytes = len(text.encode("utf-8"))
    blob = base64.b64encode(gzip.compress(text.encode("utf-8"))).decode("ascii")
    count = max(1, -(-len(blob) // args.chunk))

    lines = [
        f"# cookies.txt : {raw_bytes} bytes"
        + (f" ({len(dropped)} non-YouTube rows removed)" if dropped else ""),
        f"# encoded     : {len(blob)} characters -> {count} variable(s) of <= {args.chunk}",
        "",
    ]
    for i in range(1, count + 1):
        part = blob[(i - 1) * args.chunk: i * args.chunk]
        lines.append(f"{args.var}_{i}={part}")

    # every printed value must respect the platform cap
    for line in lines:
        if line.startswith("#") or not line:
            continue
        name, _, value = line.partition("=")
        if len(value) > 1024:
            print(f"ERROR: {name} is {len(value)} characters (cap is 1024)",
                  file=sys.stderr)
            return 1

    out = "\n".join(lines) + "\n"
    print(out, end="")
    if dropped:
        print(f"# dropped: {', '.join(dropped)}")
    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
        print(f"# written to {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
