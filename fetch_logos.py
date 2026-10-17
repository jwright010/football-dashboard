"""
Download the 32 team logos once, into static/logos/.

Serving them from your own machine means the dashboard makes no
external browser requests at runtime: no CDN dependency, nothing for a
corporate proxy to block, and no silent breakage if ESPN changes its
paths. Thirty-two PNGs is a couple of hundred kilobytes.

Run once:

    python fetch_logos.py

Re-run only if a team rebrands or a file goes missing. Existing files
are skipped unless you pass --force.
"""

import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import httpx

import db

CDN = "https://a.espncdn.com/i/teamlogos/nfl/500"
HEADERS = {"User-Agent": "football-dashboard/1.0"}
OUT = Path(__file__).parent / "static" / "logos"


def main():
    force = "--force" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)

    db.init()
    conn = db.connect()
    try:
        teams = [
            r["abbrev_canonical"]
            for r in conn.execute(
                "SELECT abbrev_canonical FROM team ORDER BY abbrev_canonical"
            ).fetchall()
        ]
    finally:
        conn.close()

    saved, skipped, failed = 0, 0, []

    with httpx.Client(timeout=30.0, headers=HEADERS,
                      follow_redirects=True) as client:
        for abbrev in teams:
            target = OUT / f"{abbrev}.png"
            if target.exists() and not force:
                skipped += 1
                continue

            url = f"{CDN}/{abbrev.lower()}.png"
            try:
                response = client.get(url)
                if response.status_code != 200:
                    failed.append(f"{abbrev}: HTTP {response.status_code}")
                    continue
                target.write_bytes(response.content)
                saved += 1
            except Exception as exc:
                failed.append(f"{abbrev}: {type(exc).__name__}: {exc}")

    print(f"Saved {saved}, skipped {skipped} already present")
    if failed:
        print(f"\n{len(failed)} failed:")
        for line in failed:
            print(f"  {line}")
        print(
            "\nA missing logo is not fatal. The dashboard hides the image "
            "and shows the row normally."
        )

    have = len(list(OUT.glob("*.png")))
    print(f"\n{have} of {len(teams)} logos in {OUT}")
    return 0 if have else 1


if __name__ == "__main__":
    sys.exit(main())
