"""One-time: store your YouTube Music login in the secret store as YTMUSIC_AUTH.

Copy the request headers of a music.youtube.com "browse" request from your browser's
DevTools first (see README). This reads them off the clipboard, checks they work and
stores them, so the cookie never lands on disk or on screen.
"""

import json
import shutil
import subprocess
import sys

from ytmusicapi import YTMusic, setup

if not shutil.which("secrets"):
    sys.exit("This stores the login with a `secrets` command, and there isn't one. See the README for the alternative.")
paste = ["wl-paste", "--no-newline"] if shutil.which("wl-paste") else ["xclip", "-o", "-selection", "clipboard"]
headers = subprocess.run(paste, capture_output=True, text=True).stdout
try:
    auth = setup(headers_raw=headers)
    subscriptions = YTMusic(auth).get_library_subscriptions(limit=25)
except Exception as e:
    sys.exit(f"That didn't work: {e}")
subprocess.run(["secrets", "set", "YTMUSIC_AUTH"], input=json.dumps(json.loads(auth)), text=True, check=True)
print(f"Connected: YouTube Music shows {len(subscriptions)} subscribed artists on the first page.")
print("Click I'm Feeling Lucky again; it restarts itself to pick up the login.")
