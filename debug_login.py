#!/usr/bin/env python3
"""Debug-Script: Sucht nach Login-AJAX-Mechanismus in TurkTorrent."""
import requests, re

fs_api = "http://192.168.178.76:30198/v1"
sess_name = "debug_login_ajax"

requests.post(fs_api, json={"cmd": "sessions.create", "session": sess_name}, timeout=15)

print("Lade TurkTorrent Seite via FlareSolverr...")
resp = requests.post(fs_api, json={
    "cmd": "request.get",
    "url": "https://turktorrent.us/",
    "session": sess_name,
    "maxTimeout": 60000,
}, timeout=90)

data = resp.json()
html = data.get("solution", {}).get("response", "")
print(f"HTML Laenge: {len(html)}")

# Alle Script-Bloecke extrahieren
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)

# Suche nach allen JS die mit Login/Form zu tun haben
for i, script in enumerate(scripts):
    script = script.strip()
    if not script:
        continue
    sl = script.lower()
    if any(kw in sl for kw in ['loginbox', 'submit', 'form', 'ajax', 'post', 'login']):
        print(f"\n=== Script #{i+1} ({len(script)} chars) ===")
        print(script[:3000])
        if len(script) > 3000:
            print(f"... ({len(script) - 3000} more chars)")

# Auch nach externen Script-URLs suchen
ext_scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
print(f"\n=== Externe Scripts ({len(ext_scripts)}) ===")
for s in ext_scripts:
    if 'jquery' not in s.lower():
        print(f"  {s}")

# Suche nach loginbox_form im HTML
form_match = re.search(r'<form[^>]*id=["\']loginbox_form["\'][^>]*>', html, re.IGNORECASE)
if form_match:
    start = form_match.start()
    print(f"\n=== Login Form HTML ===")
    print(html[start:start+2000])

requests.post(fs_api, json={"cmd": "sessions.destroy", "session": sess_name}, timeout=5)
print("\nDone.")
