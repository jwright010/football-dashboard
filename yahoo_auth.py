"""
Yahoo OAuth 2.0 setup and token management.

Run once interactively:

    python yahoo_auth.py

That writes yahoo_token.json. After that, everything refreshes on its
own and you should not need to run this again unless you revoke access.

Yahoo requires an HTTPS redirect URI but will happily redirect to a
port nothing is listening on. The browser shows a connection error and
the authorization code sits in the address bar, which is what we read.
"""

import base64
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import httpx
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
TOKEN_FILE = Path(__file__).parent / "yahoo_token.json"

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")
REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI", "https://localhost:8000/callback")

# Refresh this far before actual expiry so a long request cannot
# straddle the boundary.
EXPIRY_MARGIN_SECONDS = 120


def _basic_auth_header():
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def build_auth_url():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "language": "en-us",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def extract_code(pasted):
    """Accept either a bare code or the full redirect URL."""
    text = (pasted or "").strip().strip('"').strip("'")
    if not text:
        return None
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        values = urllib.parse.parse_qs(query).get("code")
        return values[0] if values else None
    return text


def save_tokens(payload):
    payload = dict(payload)
    payload["obtained_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(payload, indent=2))
    return payload


def load_tokens():
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError:
        return None


def exchange_code(code):
    response = httpx.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({response.status_code}): {response.text}"
        )
    return save_tokens(response.json())


def refresh_tokens(refresh_token):
    response = httpx.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "redirect_uri": REDIRECT_URI,
            "refresh_token": refresh_token,
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed ({response.status_code}): {response.text}"
        )
    payload = response.json()
    # Yahoo does not always return a new refresh token. Keep the old one.
    payload.setdefault("refresh_token", refresh_token)
    return save_tokens(payload)


def get_access_token():
    """Return a valid access token, refreshing if needed.

    Raises if no token file exists, which means yahoo_auth.py has not
    been run yet.
    """
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError(
            "No yahoo_token.json found. Run: python yahoo_auth.py"
        )

    expires_in = int(tokens.get("expires_in", 3600))
    obtained_at = int(tokens.get("obtained_at", 0))
    age = time.time() - obtained_at

    if age < expires_in - EXPIRY_MARGIN_SECONDS:
        return tokens["access_token"]

    tokens = refresh_tokens(tokens["refresh_token"])
    return tokens["access_token"]


def main():
    missing = [
        name for name, value in (
            ("YAHOO_CLIENT_ID", CLIENT_ID),
            ("YAHOO_CLIENT_SECRET", CLIENT_SECRET),
        ) if not value
    ]
    if missing:
        print("Missing from .env: " + ", ".join(missing))
        return 1

    existing = load_tokens()
    if existing:
        print("yahoo_token.json already exists. Testing refresh.")
        try:
            refresh_tokens(existing["refresh_token"])
            print("Refresh worked. Nothing else to do.")
            return 0
        except Exception as exc:
            print(f"Refresh failed: {exc}")
            print("Continuing with a fresh authorization.\n")

    print("Step 1. Open this URL in your browser:\n")
    print(build_auth_url())
    print(
        "\nStep 2. Sign in and click Agree.\n"
        "\nStep 3. The browser will fail to load a page. That is expected.\n"
        "        Copy the entire URL from the address bar. It looks like:\n"
        "        https://localhost:8000/callback?code=abcd1234\n"
    )

    pasted = input("Step 4. Paste it here and press Enter: ")
    code = extract_code(pasted)
    if not code:
        print("No authorization code found in that text.")
        return 1

    try:
        tokens = exchange_code(code)
    except Exception as exc:
        print(f"\nFailed: {exc}")
        return 1

    print(f"\nSaved to {TOKEN_FILE.name}")
    print(f"Access token expires in {tokens.get('expires_in')} seconds.")
    print("Refreshes happen automatically from here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
