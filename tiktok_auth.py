"""One-time TikTok login for Enlistra Reels. Saves tokens to work/tiktok_token.json.

Usage:
  export TIKTOK_CLIENT_KEY=...  TIKTOK_CLIENT_SECRET=...
  python tiktok_auth.py
Your browser opens TikTok, you approve, TikTok redirects to
https://enlistra-reels.netlify.app/callback.html which shows a code.
Paste that code here. The token file is then used (and auto-refreshed) by the poster.
"""
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from enlistra import env

env.load()

KEY = os.environ.get("TIKTOK_CLIENT_KEY")
SECRET = os.environ.get("TIKTOK_CLIENT_SECRET")
REDIRECT = "https://enlistra-reels.netlify.app/callback.html"
SCOPES = "user.info.basic,video.publish,video.upload"
TOKEN_FILE = Path("work/tiktok_token.json")

if not (KEY and SECRET):
    sys.exit("Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET first.")

state = secrets.token_urlsafe(12)
auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
    "client_key": KEY, "scope": SCOPES, "response_type": "code",
    "redirect_uri": REDIRECT, "state": state})
print("Opening TikTok in your browser. If it does not open, visit:\n" + auth_url + "\n")
webbrowser.open(auth_url)
CODE_FILE = Path("work/tiktok_code.txt")
if len(sys.argv) > 1:
    code = sys.argv[1]
else:
    print(f"Waiting for the code: paste it here, or save it to {CODE_FILE}")
    import select
    code = ""
    deadline = time.time() + 900
    while time.time() < deadline and not code:
        if CODE_FILE.exists() and CODE_FILE.read_text().strip():
            code = CODE_FILE.read_text().strip()
            CODE_FILE.unlink()
            break
        r_, _, _ = select.select([sys.stdin], [], [], 2)
        if r_:
            code = sys.stdin.readline().strip()
    if not code:
        sys.exit("No code received within 15 minutes.")
code = urllib.parse.unquote(code)

r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
    "client_key": KEY, "client_secret": SECRET, "code": code,
    "grant_type": "authorization_code", "redirect_uri": REDIRECT}, timeout=30)
d = r.json()
if "access_token" not in d:
    sys.exit(f"Token exchange failed: {d}")
d["obtained_at"] = int(time.time())
TOKEN_FILE.parent.mkdir(exist_ok=True)
TOKEN_FILE.write_text(json.dumps(d, indent=2))
try:
    from enlistra import store
    store.get().set_setting("tiktok_token", d)
except Exception as e:  # noqa: BLE001
    print("token kept locally only:", e)
me = requests.get("https://open.tiktokapis.com/v2/user/info/?fields=display_name,open_id",
                  headers={"Authorization": f"Bearer {d['access_token']}"}, timeout=30).json()
name = me.get("data", {}).get("user", {}).get("display_name", "?")
print(f"\nConnected as: {name}\nSaved to {TOKEN_FILE}. The poster refreshes it automatically.")
