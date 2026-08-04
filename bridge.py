#!/usr/bin/env python3
"""
Türk ARR Bridge - Torznab Proxy für türkische Serien/Filme

Problem: Sonarr/Radarr suchen mit dem internationalen Titel (z.B. "Deeply"),
aber türkische Indexer kennen nur den Originaltitel (z.B. "İlk ve Son").

Lösung: Diese Bridge sitzt zwischen Sonarr/Radarr und Jackett/dem Indexer.
Sie fängt Suchanfragen ab, übersetzt den Titel in den türkischen Originaltitel
(+ Varianten ohne Sonderzeichen) und leitet die erweiterte Suche weiter.
"""

import os
import re
import time
import logging
import hashlib
import json
import urllib.parse
import threading
from typing import Optional
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import requests
from flask import Flask, request, Response, jsonify, render_template_string, send_file, session, redirect
from lxml import etree
try:
    from dotenv import load_dotenv
    # .env im selben Verzeichnis wie bridge.py laden
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass  # python-dotenv nicht installiert – Env-Vars müssen anderweitig gesetzt sein

# ============================================================
# Konfiguration – Persistent (JSON-Datei) + Umgebungsvariablen
# ============================================================

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/bridge_config.json")

_DEFAULT_CONFIG = {
    "sonarr_url": "",
    "sonarr_api_key": "",
    "radarr_url": "",
    "radarr_api_key": "",
    "jackett_url": "",
    "jackett_api_key": "",
    "upstream_torznab_url": "",
    "qbit_url": "",
    "qbit_user": "",
    "qbit_pass": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "boxset_auto_download": True,
    "boxset_prefer_seeders": False,
    "telegram_enabled": True,
    "bridge_port": 9696,
    "cache_ttl_seconds": 300,
    "log_level": "INFO",
    "gui_user": "",
    "gui_pass": "",
    "turktorrent_username": "",
    "turktorrent_password": "",
    "turktorrent_cookie_auto_refresh": True,
    "turktorrent_cookie_interval_minutes": 120,
    "turktorrent_site_url": "https://turktorrent.us",
    "jackett_admin_password": "",
    "turktorrent_jackett_indexer_id": "turktorrent",
    "turktorrent_last_cookie_refresh": "",
    "turktorrent_cookie_status": "",
    "turktorrent_current_cookie": "",
    "bridge_external_url": "",
    "flaresolverr_url": "",
    "arr_indexer_auto_heal": True,
    "arr_indexer_heal_interval_minutes": 15,
}


def _load_config() -> dict:
    """Lade Config: JSON-Datei > Env-Vars > Defaults.
    Wichtig: Für Felder die der User über die GUI ändert (z.B. Telegram),
    hat die JSON-Datei Vorrang über Env-Vars, damit GUI-Änderungen nicht
    bei jedem Restart von alten Env-Vars überschrieben werden.
    """
    cfg = dict(_DEFAULT_CONFIG)
    # Env-Vars als Basis (niedrigere Priorität als JSON-Datei)
    env_map = {
        "SONARR_URL": "sonarr_url", "SONARR_API_KEY": "sonarr_api_key",
        "RADARR_URL": "radarr_url", "RADARR_API_KEY": "radarr_api_key",
        "JACKETT_URL": "jackett_url", "JACKETT_API_KEY": "jackett_api_key",
        "JACKETT_ADMIN_PASSWORD": "jackett_admin_password",
        "UPSTREAM_TORZNAB_URL": "upstream_torznab_url",
        "QBIT_URL": "qbit_url", "QBIT_USER": "qbit_user", "QBIT_PASS": "qbit_pass",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "BRIDGE_PORT": "bridge_port", "CACHE_TTL_SECONDS": "cache_ttl_seconds",
        "BRIDGE_EXTERNAL_URL": "bridge_external_url",
        "LOG_LEVEL": "log_level",
        "ARR_INDEXER_AUTO_HEAL": "arr_indexer_auto_heal",
        "ARR_INDEXER_HEAL_INTERVAL_MINUTES": "arr_indexer_heal_interval_minutes",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            if cfg_key in (
                "bridge_port", "cache_ttl_seconds",
                "arr_indexer_heal_interval_minutes",
            ):
                cfg[cfg_key] = int(val)
            elif cfg_key == "arr_indexer_auto_heal":
                cfg[cfg_key] = str(val).strip().lower() in ("1", "true", "yes", "on")
            else:
                cfg[cfg_key] = val
    # JSON-Datei hat höchste Priorität (GUI-Änderungen überschreiben Env-Vars)
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if v is not None and v != ""})
    except Exception as e:
        print(f"[CONFIG] Fehler beim Laden von {CONFIG_FILE}: {e}")
    return cfg


def _save_config(cfg: dict):
    """Speichere Config als JSON-Datei."""
    try:
        Path(CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[CONFIG] Fehler beim Speichern: {e}")


# Globale Config laden
_config = _load_config()

SONARR_URL = _config["sonarr_url"]
SONARR_API_KEY = _config["sonarr_api_key"]
RADARR_URL = _config["radarr_url"]
RADARR_API_KEY = _config["radarr_api_key"]
JACKETT_URL = _config["jackett_url"]
JACKETT_API_KEY = _config["jackett_api_key"]
UPSTREAM_TORZNAB_URL = _config["upstream_torznab_url"]
BRIDGE_PORT = _config["bridge_port"]
CACHE_TTL_SECONDS = int(_config["cache_ttl_seconds"])
LOG_LEVEL = _config["log_level"]
QBIT_URL = _config["qbit_url"]
QBIT_USER = _config["qbit_user"]
QBIT_PASS = _config["qbit_pass"]
TELEGRAM_BOT_TOKEN = _config["telegram_bot_token"]
TELEGRAM_CHAT_ID = _config["telegram_chat_id"]
BOXSET_AUTO_DOWNLOAD = _config.get("boxset_auto_download", True)
# Sicherheitsgrenze: Die Bridge ist ein Suchproxy und darf keine Downloads
# ausloesen. Automatische Boxsets werden ausschliesslich vom Bot mit seiner
# zusaetzlichen Serienidentitaetspruefung verarbeitet.
BRIDGE_DIRECT_DOWNLOADS_ENABLED = False
BOXSET_PREFER_SEEDERS = _config.get("boxset_prefer_seeders", False)
TELEGRAM_ENABLED = _config.get("telegram_enabled", True)
TURKTORRENT_USERNAME = _config.get("turktorrent_username", "")
TURKTORRENT_PASSWORD = _config.get("turktorrent_password", "")
TURKTORRENT_COOKIE_AUTO_REFRESH = _config.get("turktorrent_cookie_auto_refresh", True)
TURKTORRENT_COOKIE_INTERVAL = int(_config.get("turktorrent_cookie_interval_minutes", 120))
TURKTORRENT_SITE_URL = _config.get("turktorrent_site_url", "https://turktorrent.us")
TURKTORRENT_JACKETT_INDEXER_ID = _config.get("turktorrent_jackett_indexer_id", "turktorrent")
FLARESOLVERR_URL = _config.get("flaresolverr_url", "")

# ============================================================
# TurkTorrent Cookie-Auto-Refresh (via FlareSolverr + manuelles hCaptcha per Telegram)
# ============================================================

_cookie_refresh_lock = threading.Lock()
_cookie_refresh_thread: Optional[threading.Thread] = None
_login_attempt_lock = threading.Lock()

# Manuelles hCaptcha: Bridge wartet auf Token vom User (via Telegram-Link)
_pending_captcha_token: Optional[str] = None
_pending_captcha_event = threading.Event()
_captcha_request_active = False  # True wenn auf Captcha gewartet wird
_DEFAULT_TURKTORRENT_HCAPTCHA_SITEKEY = "18b46fe7-6021-408e-b14c-f318dbae672a"
_pending_captcha_sitekey = _DEFAULT_TURKTORRENT_HCAPTCHA_SITEKEY
_pending_captcha_host = "turktorrent.us"

_TURKTORRENT_PERSISTENT_COOKIE_NAMES = {
    "uid", "pass", "c_secure_uid", "c_secure_pass",
    "member_id", "memberid", "member_hash", "membername",
    "bb_userid", "bb_password", "bbuserid", "bbpassword",
    "login_key", "remember", "rememberme", "tsue_member", "tsue_pass",
}

_TURKTORRENT_VOLATILE_COOKIE_NAMES = {
    "cf_clearance", "__cf_bm", "cf_chl_2", "cf_chl_prog",
    "phpsessid", "session", "sessionid", "ci_session",
}


def _extract_hcaptcha_sitekey(page_html: str) -> str:
    """Liest den aktuellen öffentlichen hCaptcha-Sitekey aus dem Tracker-HTML."""
    if not page_html:
        return ""
    patterns = (
        r'data-sitekey\s*=\s*["\']([A-Za-z0-9_-]{20,100})["\']',
        r'(?:sitekey|siteKey)\s*[:=]\s*["\']([A-Za-z0-9_-]{20,100})["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _request_manual_captcha(site_url: str, timeout_minutes: int = 10,
                            sitekey: str = "") -> dict:
    """
    Fordert den User per Telegram auf, hCaptcha manuell zu lösen.
    Wartet bis der Token über /captcha-callback eingeht.
    Gibt {"ok": bool, "token": str, "error": str} zurück.
    """
    global _pending_captcha_token, _captcha_request_active
    global _pending_captcha_sitekey, _pending_captcha_host
    _pending_captcha_token = None
    _pending_captcha_event.clear()
    _pending_captcha_sitekey = sitekey or _DEFAULT_TURKTORRENT_HCAPTCHA_SITEKEY
    _pending_captcha_host = (
        urllib.parse.urlparse(site_url).hostname or "turktorrent.us"
    )
    _captcha_request_active = True

    try:
        # Bridge-URL ermitteln (für den Telegram-Link)
        bridge_host = _config.get("bridge_external_url", "").rstrip("/")
        if not bridge_host:
            # Fallback: lokale IP + Port
            # Fallback: versuche eigene IP zu ermitteln
            import socket as _sock
            try:
                _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                _s.connect(("8.8.8.8", 80))
                _local_ip = _s.getsockname()[0]
                _s.close()
            except Exception:
                _local_ip = "127.0.0.1"
            bridge_host = f"http://{_local_ip}:{_config.get('bridge_port', 9696)}"

        # Telegram-/WebView-Caches dürfen keine alte inaktive Captcha-Seite
        # wiederverwenden. Jede Anforderung erhält deshalb eine eindeutige URL.
        captcha_url = f"{bridge_host}/captcha?request={int(time.time())}"
        print(f"[CAPTCHA] hCaptcha-Lösung benötigt! Link: {captcha_url}")

        # Telegram-Nachricht senden
        _send_telegram_alert(
            f"🔐 <b>hCaptcha-Lösung benötigt!</b>\n\n"
            f"TurkTorrent Session abgelaufen.\n"
            f"Bitte Captcha lösen (max. {timeout_minutes} Min):\n\n"
            f"👉 <a href=\"{captcha_url}\">Captcha jetzt lösen</a>"
        )

        # Warte auf Token (max. timeout_minutes)
        print(f"[CAPTCHA] Warte auf manuelle Lösung (max. {timeout_minutes} Min)...")
        resolved = _pending_captcha_event.wait(timeout=timeout_minutes * 60)

        if resolved and _pending_captcha_token:
            token = _pending_captcha_token
            print(f"[CAPTCHA] ✅ Token erhalten! ({token[:30]}...)")
            return {"ok": True, "token": token, "error": ""}
        else:
            print("[CAPTCHA] ❌ Timeout – keine Lösung erhalten.")
            _send_telegram_alert(f"⏰ <b>Captcha-Timeout!</b>\nKeine Lösung innerhalb von {timeout_minutes} Minuten erhalten.")
            return {"ok": False, "token": "", "error": f"Captcha-Timeout nach {timeout_minutes} Minuten – keine manuelle Lösung erhalten"}
    except Exception as e:
        return {"ok": False, "token": "", "error": str(e)[:200]}
    finally:
        _captcha_request_active = False
        _pending_captcha_token = None
        _pending_captcha_event.clear()


def _turktorrent_login(username: str, password: str, site_url: str,
                       flaresolverr_url: str = "", captcha_token: str = "") -> dict:
    """Fuehrt hoechstens einen Tracker-Login/Captcha-Dialog gleichzeitig aus.

    GUI-Test, manueller Refresh und Auto-Refresh koennen denselben Login fast
    zeitgleich starten. Da hCaptcha-Tokens nur einmal verwendbar sind, darf ein
    zweiter Versuch den gemeinsamen Callback-Zustand nicht zuruecksetzen.
    """
    if not _login_attempt_lock.acquire(blocking=False):
        return {
            "ok": False,
            "cookie": "",
            "user_agent": "",
            "error": "Ein TurkTorrent Login/Captcha-Versuch läuft bereits",
            "already_running": True,
        }

    try:
        return _turktorrent_login_once(
            username, password, site_url, flaresolverr_url, captcha_token
        )
    finally:
        _login_attempt_lock.release()


def _turktorrent_login_once(username: str, password: str, site_url: str,
                            flaresolverr_url: str = "",
                            captcha_token: str = "") -> dict:
    """
    Loggt sich bei TurkTorrent ein:
    1. FlareSolverr holt cf_clearance Cookie (Cloudflare umgehen)
    2. hCaptcha wird manuell gelöst (per Telegram-Link) oder Token direkt übergeben
    3. Direkter HTTP-POST (requests) mit cf_clearance + stKey + Captcha-Token
       → FlareSolverr's request.post kann nicht verwendet werden, da dessen data:text/html-Trick
         den Seitenkontext verliert und TurkTorrent den Login ignoriert.
    4. Cookies aus Set-Cookie-Headers extrahieren
    Gibt {"ok": bool, "cookie": str, "user_agent": str, "error": str} zurück.
    """
    if not flaresolverr_url:
        flaresolverr_url = _config.get("flaresolverr_url", "")

    if not flaresolverr_url:
        return {"ok": False, "cookie": "", "user_agent": "", "error": "FlareSolverr URL nicht konfiguriert – bitte in der GUI unter Einstellungen eintragen"}

    fs_api = f"{flaresolverr_url.rstrip('/')}/v1"
    session_id = f"turktorrent_{int(time.time())}"

    try:
        # ── Schritt 1: FlareSolverr → cf_clearance Cookie + User-Agent holen ──
        print("[COOKIE] Schritt 1: FlareSolverr – Cloudflare-Challenge lösen...")
        requests.post(fs_api, json={"cmd": "sessions.create", "session": session_id}, timeout=15)

        resp1 = requests.post(fs_api, json={
            "cmd": "request.get",
            "url": site_url.rstrip("/") + "/",
            "session": session_id,
            "maxTimeout": 60000,
        }, timeout=90)

        if not resp1.ok:
            _cleanup_flaresolverr_session(fs_api, session_id)
            return {"ok": False, "cookie": "", "user_agent": "", "error": f"FlareSolverr GET fehlgeschlagen: HTTP {resp1.status_code}"}

        data1 = resp1.json()
        if data1.get("status") != "ok":
            _cleanup_flaresolverr_session(fs_api, session_id)
            return {"ok": False, "cookie": "", "user_agent": "", "error": f"Cloudflare-Challenge fehlgeschlagen: {data1.get('message', 'Unbekannt')[:100]}"}

        solution1 = data1.get("solution", {})
        user_agent = solution1.get("userAgent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        fs_cookies = {c["name"]: c["value"] for c in solution1.get("cookies", []) if c.get("name") and c.get("value")}
        cf_clearance = fs_cookies.get("cf_clearance", "")
        page_html = solution1.get("response", "")
        captcha_sitekey = _extract_hcaptcha_sitekey(page_html)

        if not cf_clearance:
            _cleanup_flaresolverr_session(fs_api, session_id)
            return {"ok": False, "cookie": "", "user_agent": "", "error": "FlareSolverr hat kein cf_clearance Cookie geliefert"}

        print(f"[COOKIE] cf_clearance erhalten: {cf_clearance[:30]}...")

        # stKey aus der Seite extrahieren (CSRF-Token, benötigt für Login)
        stkey_match = re.search(r'stKey:\s*"([^"]+)"', page_html)
        stkey_from_flare = stkey_match.group(1) if stkey_match else ""
        print(f"[COOKIE] stKey aus FlareSolverr-Seite: {stkey_from_flare}")

        # FlareSolverr-Session schließen (wird nicht mehr benötigt)
        _cleanup_flaresolverr_session(fs_api, session_id)

        # ── Schritt 1b: Frischen stKey per direktem GET holen ──
        # Der stKey ist IP- und zeitbasiert. Wir holen einen frischen per requests.
        print("[COOKIE] Schritt 1b: Frischen stKey per direktem GET holen...")
        http_session = requests.Session()
        http_session.headers.update({
            "User-Agent": user_agent,
            "Referer": site_url.rstrip("/") + "/",
            "Origin": site_url.rstrip("/"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "tr,en-US;q=0.9,en;q=0.8,de;q=0.7",
        })
        http_session.cookies.set("cf_clearance", cf_clearance, domain=".turktorrent.us")

        resp_get = http_session.get(site_url.rstrip("/") + "/", timeout=20)
        stkey = ""
        if resp_get.ok and "challenge" not in resp_get.text[:500].lower():
            captcha_sitekey = (
                _extract_hcaptcha_sitekey(resp_get.text) or captcha_sitekey
            )
            stkey_match2 = re.search(r'stKey:\s*"([^"]+)"', resp_get.text)
            if stkey_match2:
                stkey = stkey_match2.group(1)
                print(f"[COOKIE] Frischer stKey: {stkey}")
            else:
                print("[COOKIE] WARNUNG: Kein stKey im HTML gefunden, verwende FlareSolverr-stKey")
                stkey = stkey_from_flare
        else:
            # Cloudflare blockiert den direkten GET → FlareSolverr-stKey verwenden
            print("[COOKIE] Cloudflare blockiert direkten GET, verwende FlareSolverr-stKey")
            stkey = stkey_from_flare

        if not stkey:
            return {"ok": False, "cookie": "", "user_agent": "", "error": "Kein stKey (CSRF-Token) extrahiert – Login nicht möglich"}

        # ── Schritt 2: hCaptcha lösen (manuell per Telegram oder direkt übergeben) ──
        if captcha_token:
            print(f"[COOKIE] Schritt 2: hCaptcha-Token direkt übergeben ({captcha_token[:30]}...)")
            captcha_result = {"ok": True, "token": captcha_token, "error": ""}
        else:
            print("[COOKIE] Schritt 2: hCaptcha manuell lösen (Telegram)...")
            captcha_result = _request_manual_captcha(
                site_url, sitekey=captcha_sitekey
            )

        if not captcha_result["ok"]:
            return {"ok": False, "cookie": "", "user_agent": "", "error": f"hCaptcha lösen fehlgeschlagen: {captcha_result['error']}"}

        hcaptcha_token = captcha_result["token"]

        # ── Schritt 3: AJAX-Login-POST an /ajax/login.php ──
        # TurkTorrent TSUE nutzt jQuery AJAX für den Login (NICHT normalen Form-POST!):
        #   $.ajax({url: '/ajax/login.php', data: 'action=login&...&securitytoken=stKey&captcha=...', ...})
        # Die Cookies werden NICHT per Set-Cookie gesetzt, sondern der Server
        # antwortet mit einer Erfolgs-/Fehlermeldung. Bei Erfolg macht das JS window.location.reload()
        # und der Server erkennt den Login über die interne Session.
        print("[COOKIE] Schritt 3: AJAX-Login-POST an /ajax/login.php...")
        login_url = site_url.rstrip("/") + "/ajax/login.php"
        build_query = (
            "action=login"
            "&loginbox_remember=true"
            "&loginbox_membername=" + urllib.parse.quote(username, safe='')
            + "&loginbox_password=" + urllib.parse.quote(password, safe='')
            + "&securitytoken=" + stkey
            + "&captcha=" + urllib.parse.quote(hcaptcha_token, safe='')
        )

        # POST senden (wie jQuery AJAX)
        resp_login = http_session.post(
            login_url,
            data=build_query,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
            allow_redirects=False,
        )

        print(f"[COOKIE] POST Status: {resp_login.status_code}")

        # Set-Cookie-Headers extrahieren (Login-Cookies kommen als Set-Cookie!)
        set_cookie_headers = resp_login.raw.headers.getlist("Set-Cookie")
        print(f"[COOKIE] POST Set-Cookie Headers ({len(set_cookie_headers)}):")
        for sc in set_cookie_headers[:10]:
            print(f"[COOKIE]   {sc[:120]}")

        # AJAX-Response analysieren
        ajax_response = resp_login.text
        print(f"[COOKIE] AJAX-Response ({len(ajax_response)} Zeichen): {ajax_response[:300]}")

        # Prüfe auf TSUE AJAX-Fehler: "-ERROR-<div class='error'...>..."
        if "-ERROR-" in ajax_response:
            # Fehlermeldung extrahieren
            error_match = re.search(r'<div[^>]*class=["\']error["\'][^>]*[^>]*>([^<]+(?:<br\s*/?>([^<]+))?)', ajax_response, re.DOTALL)
            if error_match:
                error_text = re.sub(r'<[^>]+>', ' ', error_match.group(0)).strip()
            else:
                error_text = re.sub(r'<[^>]+>', ' ', ajax_response.replace("-ERROR-", "")).strip()
            print(f"[COOKIE] ❌ TurkTorrent AJAX-Fehler: {error_text[:200]}")
            return {"ok": False, "cookie": "", "user_agent": user_agent, "error": f"TurkTorrent: {error_text[:200]}"}

        # Bei Erfolg: Server setzt Login-Cookies via Set-Cookie Headers
        # Zusätzlich GET an Homepage um Cookies zu bestätigen und TSUE-Status zu prüfen
        print("[COOKIE] AJAX-Login-Antwort scheint OK! Lade Homepage für Bestätigung...")
        resp_home = http_session.get(site_url.rstrip("/") + "/", timeout=20, allow_redirects=True)
        response_html = resp_home.text
        response_url = str(resp_home.url)

        # Weitere Set-Cookie von Homepage-GET
        home_cookies = resp_home.raw.headers.getlist("Set-Cookie")
        if home_cookies:
            print(f"[COOKIE] Homepage Set-Cookie ({len(home_cookies)}):")
            for sc in home_cookies[:10]:
                print(f"[COOKIE]   {sc[:120]}")

        # Alle Cookies aus der HTTP-Session sammeln
        session_cookies = dict(http_session.cookies)
        print(f"[COOKIE] Session-Cookies nach Login: {list(session_cookies.keys())}")
        for k, v in session_cookies.items():
            print(f"[COOKIE]   {k} = {v[:40]}...")

        # TSUE-Variablen aus HTML extrahieren (Login-Indikator)
        _member_id_match = re.search(r'memberid:\s*"([^"]+)"', response_html)
        _member_name_match = re.search(r'membername:\s*"([^"]+)"', response_html)
        _stkey_match = re.search(r'stKey:\s*"([^"]+)"', response_html)
        _tsue_memberid = _member_id_match.group(1) if _member_id_match else "?"
        _tsue_membername = _member_name_match.group(1) if _member_name_match else "?"
        _tsue_stkey = _stkey_match.group(1) if _stkey_match else "?"
        print(f"[COOKIE] TSUE memberid={_tsue_memberid}, membername={_tsue_membername}")
        print(f"[COOKIE] TSUE stKey={_tsue_stkey[:50]}...")

        # Alle Cookies zusammenführen (FlareSolverr cf_clearance + Session-Cookies)
        all_cookies = {}
        all_cookies.update(fs_cookies)          # cf_clearance etc.
        all_cookies.update(session_cookies)     # Login-Cookies aus HTTP-Session (uid, pass, etc.)

        print(f"[COOKIE] Alle Cookies: {list(all_cookies.keys())}")

        # Prüfen ob Login erfolgreich war
        has_login_cookie = any(k in ("uid", "pass", "c_secure_uid", "c_secure_pass", "tsue_member") for k in all_cookies)

        # TSUE-basierte Login-Erkennung (stärkster Indikator):
        # Nach dem Login zeigt TurkTorrent memberid != "0" und membername != "Guest"
        tsue_logged_in = (_tsue_memberid not in ("0", "?") and _tsue_membername not in ("Guest", "?"))

        is_logged_in = False
        if has_login_cookie:
            is_logged_in = True
            print("[COOKIE] ✅ Login erkannt via Login-Cookies")
        elif tsue_logged_in:
            is_logged_in = True
            print(f"[COOKIE] ✅ Login erkannt via TSUE memberid={_tsue_memberid}, membername={_tsue_membername}")
        elif "member.php?action=logout" in response_html.lower() or "çıkış" in response_html.lower():
            is_logged_in = True
            print("[COOKIE] ✅ Login erkannt via Logout-Link auf der Seite")
        elif username.lower() in response_html.lower():
            is_logged_in = True
            print(f"[COOKIE] ✅ Login erkannt via Username '{username}' auf der Seite")

        # Zusätzliche Fehlererkennung (Türkisch: "Hatalı" = "Falsch/Fehler")
        if "Hatalı" in response_html or "hatal" in response_html.lower():
            error_match = re.search(r'class=["\']error["\'][^>]*>([^<]+)', response_html)
            error_msg = error_match.group(1).strip() if error_match else "Unbekannter Fehler von TurkTorrent"
            print(f"[COOKIE] ❌ TurkTorrent Fehlermeldung: {error_msg}")
            return {"ok": False, "cookie": "", "user_agent": "", "error": f"TurkTorrent: {error_msg}"}

        if not is_logged_in:
            snippet = response_html[:500].replace('\n', ' ').replace('\r', '')
            print(f"[COOKIE] ❌ Login fehlgeschlagen. Keine Login-Cookies und keine Logout-Links gefunden.")
            print(f"[COOKIE] HTML-Snippet: {snippet[:300]}")
            return {"ok": False, "cookie": "", "user_agent": "", "error": "Login fehlgeschlagen – keine Login-Bestätigung auf der Seite"}

        if not has_login_cookie:
            print(f"[COOKIE] ⚠️ Warnung: Keine typischen Login-Cookies, aber Login auf Seite erkannt")
            print(f"[COOKIE] Vorhandene Cookies: {list(all_cookies.keys())}")

        persistent_cookies = _filter_persistent_turktorrent_cookies(all_cookies)
        dropped_cookie_names = [name for name in all_cookies.keys() if name not in persistent_cookies]
        if dropped_cookie_names:
            print(f"[COOKIE] Entferne kurzlebige Cookies vor dem Speichern: {dropped_cookie_names}")

        cookie_str = "; ".join(f"{k}={v}" for k, v in persistent_cookies.items())
        print(f"[COOKIE] ✅ Login erfolgreich! {len(persistent_cookies)} persistente Cookies gespeichert: {list(persistent_cookies.keys())}")
        return {"ok": True, "cookie": cookie_str, "user_agent": user_agent, "error": ""}

    except requests.exceptions.ConnectionError:
        try:
            _cleanup_flaresolverr_session(fs_api, session_id)
        except Exception:
            pass
        return {"ok": False, "cookie": "", "user_agent": "", "error": f"FlareSolverr nicht erreichbar unter {flaresolverr_url}"}
    except requests.exceptions.Timeout:
        try:
            _cleanup_flaresolverr_session(fs_api, session_id)
        except Exception:
            pass
        return {"ok": False, "cookie": "", "user_agent": "", "error": "Timeout beim Login"}
    except Exception as e:
        try:
            _cleanup_flaresolverr_session(fs_api, session_id)
        except Exception:
            pass
        return {"ok": False, "cookie": "", "user_agent": "", "error": str(e)[:200]}


def _cleanup_flaresolverr_session(fs_api: str, session_id: str):
    """Räumt eine FlareSolverr-Session auf."""
    try:
        requests.post(fs_api, json={
            "cmd": "sessions.destroy",
            "session": session_id,
        }, timeout=5)
    except Exception:
        pass


def _check_tsue_logged_in(html: str) -> bool:
    """Prüft anhand von TSUE-Variablen im HTML ob der User eingeloggt ist."""
    # TSUE setzt memberid: "0" und membername: "Guest" für nicht-eingeloggte User
    mid_match = re.search(r'memberid:\s*"([^"]+)"', html)
    mname_match = re.search(r'membername:\s*"([^"]+)"', html)
    mid = mid_match.group(1) if mid_match else "0"
    mname = mname_match.group(1) if mname_match else "Guest"

    if mid not in ("0", "?", "") and mname not in ("Guest", "?", ""):
        return True

    # Fallback: Logout-Link auf der Seite
    if "member.php?action=logout" in html.lower() or "çıkış" in html.lower():
        return True

    return False


def _filter_persistent_turktorrent_cookies(cookies: dict) -> dict:
    """
    Behalte nur langlebige Login-Cookies.

    Cloudflare- und Session-Cookies laufen häufig deutlich früher ab als die
    eigentlichen Remember-Me-Login-Cookies. Werden sie mitgespeichert, löst das
    unnötige Re-Logins aus, obwohl die Anmeldung selbst noch gültig ist.
    """
    if not cookies:
        return {}

    persistent = {}
    fallback = {}

    for name, value in cookies.items():
        lowered = name.lower()
        if lowered in _TURKTORRENT_PERSISTENT_COOKIE_NAMES:
            persistent[name] = value
            continue

        if lowered in _TURKTORRENT_VOLATILE_COOKIE_NAMES:
            continue

        if lowered.startswith("__cf") or "session" in lowered or lowered.endswith("sessid"):
            continue

        fallback[name] = value

    if persistent:
        return persistent

    if fallback:
        return fallback

    return dict(cookies)


def _cookie_refresh_due(last_refresh_iso: str, interval_minutes: int) -> bool:
    """Prüft, ob seit dem letzten erfolgreichen Login das Refresh-Intervall abgelaufen ist."""
    if interval_minutes <= 0 or not last_refresh_iso:
        return True

    try:
        last_refresh = datetime.fromisoformat(last_refresh_iso)
    except Exception:
        return True

    return datetime.now() >= last_refresh + timedelta(minutes=interval_minutes)


def _validate_turktorrent_cookie(cookie: str, site_url: str, flaresolverr_url: str = "") -> dict:
    """
    Prüft ob der TurkTorrent-Indexer in Jackett funktioniert.
    Strategie: Einen echten Jackett-Test machen (Suche + Download).
    Wenn Jackett Torrents liefern kann, ist der Cookie gültig – egal was
    unsere eigene Homepage-Prüfung sagt (Jackett hat eigenen FlareSolverr).
    Gibt {"ok": bool, "error": str, "fresh_cf_clearance": str} zurück.
    """
    if not cookie:
        return {"ok": False, "error": "Keine Cookie vorhanden", "fresh_cf_clearance": ""}

    # ── Primäre Prüfung: Jackett fragen ob der Indexer funktioniert ──
    try:
        jackett_url = _config.get("jackett_url", "")
        jackett_api_key = _config.get("jackett_api_key", "")
        jackett_admin_password = _config.get("jackett_admin_password", "")
        indexer_id = _config.get("turktorrent_jackett_indexer_id", "turktorrent")

        if jackett_url and jackett_api_key:
            # Jackett-Suche: Hole ein Ergebnis und prüfe ob Download klappt
            session = _get_jackett_session(jackett_url, jackett_admin_password)
            search_url = f"{jackett_url.rstrip('/')}/api/v2.0/indexers/{indexer_id}/results"
            resp = session.get(search_url, params={
                "apikey": jackett_api_key,
                "Query": "test",
                "Type": "search",
            }, timeout=30)

            if resp.ok:
                data = resp.json()
                results = data.get("Results", [])
                indexers = data.get("Indexers", [])

                # ── Zuerst: Prüfe ob Jackett einen Indexer-Fehler meldet ──
                # Jackett gibt Fehler im Indexers[]-Array zurück (z.B. "Login failed")
                for idx_info in indexers:
                    idx_error = idx_info.get("Error", "")
                    if idx_error:
                        # Login-Fehler → Cookie ist definitiv ungültig
                        short_err = idx_error.split("\n")[0][:120]  # Erste Zeile, max 120 Zeichen
                        print(f"[COOKIE] ❌ Jackett Indexer-Fehler: {short_err}")
                        return {"ok": False, "error": f"Jackett: {short_err}", "fresh_cf_clearance": ""}

                if len(results) > 0:
                    # Versuche einen Torrent herunterzuladen
                    dl_url = results[0].get("Link", "")
                    if dl_url:
                        dl_resp = session.get(dl_url, timeout=15, allow_redirects=True)
                        ct = dl_resp.headers.get("Content-Type", "").lower()
                        if "torrent" in ct or "octet" in ct:
                            print(f"[COOKIE] ✅ Jackett-Test OK: {len(results)} Ergebnisse, Download funktioniert ({len(dl_resp.content)} bytes)")
                            return {"ok": True, "error": "", "fresh_cf_clearance": ""}
                        else:
                            print(f"[COOKIE] ⚠️ Jackett-Download fehlgeschlagen: Content-Type={ct}")
                            return {"ok": False, "error": f"Jackett-Download fehlgeschlagen (Content-Type: {ct})", "fresh_cf_clearance": ""}
                    else:
                        # Ergebnisse vorhanden aber kein Download-Link → trotzdem OK
                        print(f"[COOKIE] ✅ Jackett-Test OK: {len(results)} Ergebnisse (kein DL-Link zum Testen)")
                        return {"ok": True, "error": "", "fresh_cf_clearance": ""}
                else:
                    # Keine Ergebnisse und kein Fehler → Cookie vermutlich OK
                    # (Suchbegriff hat einfach nichts gefunden)
                    print(f"[COOKIE] ✅ Jackett erreichbar, keine Fehler (0 Ergebnisse für 'test')")
                    return {"ok": True, "error": "", "fresh_cf_clearance": ""}
            else:
                print(f"[COOKIE] ⚠️ Jackett-Suche HTTP {resp.status_code}")
                # Jackett nicht erreichbar → können nicht prüfen → als OK behandeln
                return {"ok": True, "error": f"Jackett nicht erreichbar (HTTP {resp.status_code})", "fresh_cf_clearance": ""}
    except Exception as e:
        print(f"[COOKIE] ⚠️ Jackett-Validierung fehlgeschlagen: {e}")
        # Bei Fehler: nicht als "abgelaufen" melden → kein unnötiges Captcha
        return {"ok": True, "error": f"Jackett-Prüfung fehlgeschlagen: {str(e)[:80]}", "fresh_cf_clearance": ""}

    # Fallback wenn Jackett nicht konfiguriert: Cookie als gültig behandeln
    # (besser kein unnötiges Captcha als ständig nerven)
    return {"ok": True, "error": "Jackett nicht konfiguriert – Cookie-Status unklar", "fresh_cf_clearance": ""}


def _get_jackett_session(jackett_url: str, admin_password: str) -> requests.Session:
    """
    Erstellt eine authentifizierte Jackett-Session (Admin-Login).
    Jackett erfordert für Config-API-Zugriffe ein Admin-Cookie.
    """
    session = requests.Session()
    if admin_password:
        login_url = f"{jackett_url.rstrip('/')}/UI/Dashboard"
        resp = session.post(login_url, data={"password": admin_password}, timeout=10, allow_redirects=False)
        # Jackett gibt 302 + Set-Cookie bei erfolgreichem Login
        if "Jackett" in session.cookies.get_dict():
            print(f"[JACKETT] Admin-Login erfolgreich")
        else:
            print(f"[JACKETT] Admin-Login fehlgeschlagen (HTTP {resp.status_code})")
    return session


def _update_jackett_indexer_cookie(cookie: str, user_agent: str) -> dict:
    """
    Aktualisiert die Cookie-Konfiguration eines Jackett-Indexers über die Jackett API.
    Gibt {"ok": bool, "error": str} zurück.
    """
    try:
        jackett_url = _config.get("jackett_url", "")
        jackett_api_key = _config.get("jackett_api_key", "")
        jackett_admin_password = _config.get("jackett_admin_password", "")
        indexer_id = _config.get("turktorrent_jackett_indexer_id", "turktorrent")

        if not jackett_url or not jackett_api_key:
            return {"ok": False, "error": "Jackett URL oder API Key nicht konfiguriert"}

        # Admin-Session aufbauen (Jackett erfordert Admin-Cookie für Config-Endpoints)
        session = _get_jackett_session(jackett_url, jackett_admin_password)

        # Aktuelle Indexer-Config von Jackett holen
        config_url = f"{jackett_url.rstrip('/')}/api/v2.0/indexers/{indexer_id}/config"
        headers = {"Content-Type": "application/json"}
        params = {"apikey": jackett_api_key}

        resp = session.get(config_url, params=params, timeout=10, allow_redirects=True)
        if resp.status_code == 302:
            return {"ok": False, "error": "Jackett Admin-Login fehlgeschlagen – falsches Passwort? (jackett_admin_password prüfen)"}
        if not resp.ok:
            return {"ok": False, "error": f"Jackett Config GET HTTP {resp.status_code}: {resp.text[:100]}"}

        # HTML-Response erkennen (Login-Seite statt JSON → Admin-Passwort fehlt/falsch)
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type or resp.text.strip().startswith("<"):
            if not jackett_admin_password:
                return {"ok": False, "error": "Jackett Admin-Passwort nicht konfiguriert – bitte in der GUI unter Indexer eintragen"}
            return {"ok": False, "error": "Jackett Admin-Login fehlgeschlagen – Passwort prüfen (jackett_admin_password)"}

        try:
            config_items = resp.json()
        except Exception:
            return {"ok": False, "error": f"Jackett Config ungültiges JSON: {resp.text[:100]}"}

        # Cookie und User-Agent in der Config aktualisieren
        updated_fields = []
        for item in config_items:
            item_id = item.get("id", "").lower()
            if item_id == "cookie":
                item["value"] = cookie
                updated_fields.append("cookie")
            elif item_id in ("useragent", "user-agent", "user_agent"):
                item["value"] = user_agent
                updated_fields.append("user-agent")

        print(f"[JACKETT] Aktualisiere Felder: {updated_fields}")

        # Aktualisierte Config an Jackett senden
        resp2 = session.post(config_url, json=config_items, params=params, headers=headers, timeout=30)
        if resp2.status_code in (200, 204):
            return {"ok": True, "error": ""}

        # HTTP 500 = Jackett testet den Cookie und der Test schlägt fehl
        error_detail = ""
        try:
            err_json = resp2.json()
            # Jackett gibt den Fehler im "config" Array unter "lasterror" zurück
            for item in err_json.get("config", []):
                if item.get("id") == "lasterror":
                    error_detail = item.get("value", "")
                    break
        except Exception:
            error_detail = resp2.text[:200]

        if error_detail:
            print(f"[JACKETT] Fehler-Detail: {error_detail}")

        return {"ok": False, "error": f"Jackett Config POST HTTP {resp2.status_code}: {error_detail or resp2.text[:100]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _send_telegram_alert(message: str):
    """Sendet eine Telegram-Benachrichtigung (wenn konfiguriert)."""
    try:
        token = _config.get("telegram_bot_token", "")
        chat_id = _config.get("telegram_chat_id", "")
        enabled = _config.get("telegram_enabled", True)
        if not token or not chat_id or not enabled:
            print(f"[TELEGRAM] Übersprungen (enabled={enabled}, token={'ja' if token else 'NEIN'}, chat_id={'ja' if chat_id else 'NEIN'})")
            return
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"🍪 Turk ARR Bridge\n\n{message}", "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.ok:
            print(f"[TELEGRAM] ✅ Nachricht gesendet")
        else:
            print(f"[TELEGRAM] ❌ API-Fehler: {resp.status_code} – {resp.text[:150]}")
    except Exception as e:
        print(f"[TELEGRAM] Alert fehlgeschlagen: {e}")


def _do_cookie_refresh(force_login: bool = False):
    """Führt einen Cookie-Refresh durch: FlareSolverr Login → Jackett Update.

    Der automatische Pfad bewahrt einen noch gültigen Cookie. Der ausdrücklich
    angeforderte Captcha-Test kann mit ``force_login=True`` trotzdem einen neuen
    Login starten, damit der Telegram-/Tailscale-Flow wirklich testbar ist.
    """
    global _config
    username = _config.get("turktorrent_username", "")
    password = _config.get("turktorrent_password", "")
    site_url = _config.get("turktorrent_site_url", "https://turktorrent.us")
    flaresolverr_url = _config.get("flaresolverr_url", "")

    if not username or not password:
        _config["turktorrent_cookie_status"] = "❌ Username/Passwort nicht konfiguriert"
        _save_config(_config)
        return {"ok": False, "error": "Username/Passwort nicht konfiguriert"}

    if not flaresolverr_url:
        _config["turktorrent_cookie_status"] = "❌ FlareSolverr URL nicht konfiguriert"
        _save_config(_config)
        return {"ok": False, "error": "FlareSolverr URL nicht konfiguriert"}

    # Zuerst prüfen ob Cookie noch gültig ist
    current_cookie = _config.get("turktorrent_current_cookie", "")
    if current_cookie and not force_login:
        validation = _validate_turktorrent_cookie(current_cookie, site_url, flaresolverr_url)
        if validation["ok"]:
            print(f"[COOKIE] Cookie noch gültig – kein Refresh nötig")
            _config["turktorrent_cookie_status"] = f"✅ Cookie gültig (geprüft {datetime.now().strftime('%d.%m.%Y %H:%M')})"
            _save_config(_config)
            return {"ok": True, "error": ""}
        print(f"[COOKIE] Cookie abgelaufen: {validation['error']}")
    elif current_cookie and force_login:
        print("[COOKIE] Manueller Captcha-Test: gültigen Cookie bewusst neu anmelden")

    print(f"[COOKIE] Starte TurkTorrent Cookie-Refresh via FlareSolverr + manuelles hCaptcha für User '{username}'...")

    # Login via FlareSolverr + manuelles hCaptcha
    login_result = _turktorrent_login(username, password, site_url, flaresolverr_url)
    if not login_result["ok"]:
        if login_result.get("already_running"):
            return login_result
        msg = f"❌ Login fehlgeschlagen: {login_result['error']}"
        _config["turktorrent_cookie_status"] = msg
        _save_config(_config)
        print(f"[COOKIE] {msg}")
        _send_telegram_alert(f"❌ <b>Cookie-Refresh fehlgeschlagen</b>\n{login_result['error']}")
        return {"ok": False, "error": login_result["error"]}

    print(f"[COOKIE] Login erfolgreich. Cookie: {login_result['cookie'][:50]}...")

    # Den frisch bestaetigten Tracker-Cookie sofort sichern. Jackett prueft beim
    # Config-POST gleichzeitig seine HTML-Definition. Wenn sich dort nur ein
    # Selektor geaendert hat, kann dieser Test fehlschlagen, obwohl der Login
    # selbst erfolgreich war. Ohne die fruehe Sicherung ging der gueltige Cookie
    # bisher verloren und der Auto-Refresh startete immer neue Captchas.
    _config["turktorrent_current_cookie"] = login_result["cookie"]
    _save_config(_config)

    # Schritt 2: Jackett aktualisieren
    update_result = _update_jackett_indexer_cookie(login_result["cookie"], login_result["user_agent"])
    if not update_result["ok"]:
        msg = f"⚠️ Login OK, Jackett-Update fehlgeschlagen: {update_result['error']}"
        _config["turktorrent_cookie_status"] = msg
        _save_config(_config)
        print(f"[COOKIE] {msg}")
        _send_telegram_alert(f"⚠️ <b>Jackett-Update fehlgeschlagen</b>\nLogin war OK, aber Cookie konnte nicht in Jackett eingetragen werden.\n{update_result['error']}")
        return {"ok": False, "error": update_result["error"]}

    msg = f"✅ Cookie erfolgreich aktualisiert ({datetime.now().strftime('%d.%m.%Y %H:%M')})"
    _config["turktorrent_cookie_status"] = msg
    _config["turktorrent_last_cookie_refresh"] = datetime.now().isoformat()
    _save_config(_config)
    print(f"[COOKIE] {msg}")
    _send_telegram_alert(f"✅ <b>Cookie erfolgreich aktualisiert!</b>\nJackett wurde mit neuen TurkTorrent-Cookies versorgt.")
    # ARR kann den Indexer nach dem vorherigen Loginfehler weiterhin sperren.
    # Nach erfolgreicher Reparatur sofort eine abgesicherte Reaktivierung planen.
    _request_arr_indexer_heal()
    return {"ok": True, "error": ""}


def _cookie_refresh_loop():
    """Hintergrund-Thread: Prüft alle 5 Min ob Cookie gültig, bei Ablauf sofort Login."""
    # Beim ersten Start kurz warten bis alles initialisiert ist
    time.sleep(30)

    CHECK_INTERVAL = 300  # alle 5 Minuten prüfen
    _last_captcha_request = 0  # Cooldown: nicht ständig Telegram spammen

    while True:
        try:
            enabled = _config.get("turktorrent_cookie_auto_refresh", True)
            username = _config.get("turktorrent_username", "")
            password = _config.get("turktorrent_password", "")
            flaresolverr_url = _config.get("flaresolverr_url", "")
            site_url = _config.get("turktorrent_site_url", "https://turktorrent.us")
            current_cookie = _config.get("turktorrent_current_cookie", "")
            refresh_interval = int(_config.get("turktorrent_cookie_interval_minutes", 120) or 120)
            last_refresh = _config.get("turktorrent_last_cookie_refresh", "")

            if not (enabled and username and password and flaresolverr_url):
                time.sleep(CHECK_INTERVAL)
                continue

            # Wenn gerade ein Captcha-Request läuft, nicht stören
            if _captcha_request_active:
                print(f"[COOKIE] ⏳ Captcha-Request läuft, warte...")
                time.sleep(60)  # kürzeres Intervall, damit wir schnell reagieren
                continue

            if current_cookie and not _cookie_refresh_due(last_refresh, refresh_interval):
                print(f"[COOKIE] ⏳ Cookie noch innerhalb des Refresh-Intervalls ({refresh_interval} Min) – keine Re-Validierung nötig")
                time.sleep(CHECK_INTERVAL)
                continue

            # Cookie validieren
            if current_cookie:
                validation = _validate_turktorrent_cookie(current_cookie, site_url, flaresolverr_url)
                if validation["ok"]:
                    # Cookie noch gültig → nichts tun
                    print(f"[COOKIE] ✅ Cookie gültig (Check {datetime.now().strftime('%H:%M')})")
                    _config["turktorrent_cookie_status"] = f"✅ Cookie gültig (geprüft {datetime.now().strftime('%d.%m.%Y %H:%M')})"
                    _save_config(_config)
                    time.sleep(CHECK_INTERVAL)
                    continue

                # Cookie abgelaufen!
                print(f"[COOKIE] ⚠️ Cookie abgelaufen: {validation['error']}")
                _config["turktorrent_cookie_status"] = f"⚠️ Cookie abgelaufen: {validation['error']}"
                _save_config(_config)

            # Cooldown: nur alle 15 Min ein neues Captcha anfordern (nicht spammen)
            now = time.time()
            if now - _last_captcha_request < 900:  # 15 Min Cooldown
                remaining = int((900 - (now - _last_captcha_request)) / 60)
                print(f"[COOKIE] Captcha-Cooldown aktiv, nächster Versuch in ~{remaining} Min")
                time.sleep(CHECK_INTERVAL)
                continue

            _last_captcha_request = now

            # Login-Versuch starten (wird Telegram-Captcha anfordern)
            with _cookie_refresh_lock:
                _do_cookie_refresh()

            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"[COOKIE] Fehler im Refresh-Loop: {e}")
            time.sleep(CHECK_INTERVAL)


def _start_cookie_refresh_thread():
    """Startet den Cookie-Refresh Hintergrund-Thread."""
    global _cookie_refresh_thread
    if _cookie_refresh_thread is None or not _cookie_refresh_thread.is_alive():
        _cookie_refresh_thread = threading.Thread(target=_cookie_refresh_loop, daemon=True)
        _cookie_refresh_thread.start()
        print("[COOKIE] Auto-Refresh Thread gestartet")


# ============================================================
# Persistente Lern-Datenbank (wächst automatisch)
# ============================================================

LEARNED_DB_FILE = os.environ.get("LEARNED_DB_FILE", "/config/learned_mappings.json")

# Struktur: { "normalized_query": { "titles": [...], "source": "tvdb|sonarr|manual", "learned_at": "ISO", "search_count": N } }
_learned_db: dict = {}
_learned_db_lock = threading.Lock()


def _load_learned_db():
    global _learned_db
    try:
        if os.path.exists(LEARNED_DB_FILE):
            with open(LEARNED_DB_FILE, "r") as f:
                _learned_db = json.load(f)
            print(f"[LEARN] {len(_learned_db)} gelernte Mappings geladen")
    except Exception as e:
        print(f"[LEARN] Fehler beim Laden: {e}")
        _learned_db = {}


def _save_learned_db():
    try:
        Path(LEARNED_DB_FILE).parent.mkdir(parents=True, exist_ok=True)
        with _learned_db_lock:
            with open(LEARNED_DB_FILE, "w") as f:
                json.dump(_learned_db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[LEARN] Fehler beim Speichern: {e}")


def _add_learned(query: str, titles: list, source: str = "auto"):
    """Speichert ein neues gelerntes Mapping dauerhaft."""
    key = normalize_for_search_early(query)
    if not key or len(titles) < 2:
        return False
    with _learned_db_lock:
        existing = _learned_db.get(key)
        if existing:
            # Zähler erhöhen + neue Titel mergen
            old_set = set(existing.get("titles", []))
            new_set = set(titles)
            if new_set == old_set:
                existing["search_count"] = existing.get("search_count", 0) + 1
                return False  # Nichts Neues
            existing["titles"] = sorted(new_set | old_set)
            existing["search_count"] = existing.get("search_count", 0) + 1
            existing["updated_at"] = datetime.now().isoformat()
        else:
            _learned_db[key] = {
                "titles": sorted(set(titles)),
                "source": source,
                "learned_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "search_count": 1,
            }
    _save_learned_db()
    return True


def normalize_for_search_early(text: str) -> str:
    """Frühe Normalisierung (vor dem eigentlichen Setup)."""
    tr_map = {"İ": "I", "ı": "i", "Ş": "S", "ş": "s", "Ğ": "G", "ğ": "g",
              "Ü": "U", "ü": "u", "Ö": "O", "ö": "o", "Ç": "C", "ç": "c"}
    result = text
    for k, v in tr_map.items():
        result = result.replace(k, v)
    result = re.sub(r'[^a-z0-9\s]', '', result.lower())
    return re.sub(r'\s+', ' ', result).strip()


def _normalize_key(text: str) -> str:
    """Normalisiert Text zu lowercase ASCII für Vergleiche."""
    tr_map = {"İ": "I", "ı": "i", "Ş": "S", "ş": "s", "Ğ": "G", "ğ": "g",
              "Ü": "U", "ü": "u", "Ö": "O", "ö": "o", "Ç": "C", "ç": "c"}
    result = text
    for k, v in tr_map.items():
        result = result.replace(k, v)
    return re.sub(r'[^a-z0-9]', '', result.lower())


def _is_relevant(query: str, entry: dict) -> bool:
    """
    Prüft ob ein Sonarr/Radarr Lookup-Treffer tatsächlich zum Suchbegriff passt.
    Vermeidet z.B. dass "Cukur" → "Cukrárna" oder "Precious Pupp" ergibt.
    """
    q = _normalize_key(query)
    if len(q) < 2:
        return False

    # Prüfe title, originalTitle und alle alternateTitles
    candidates = []
    t = entry.get("title", "")
    if t:
        candidates.append(t)
    orig = entry.get("originalTitle", "") or ""
    if orig:
        candidates.append(orig)
    for a in entry.get("alternateTitles", []):
        at = a.get("title", "")
        if at:
            candidates.append(at)
            # aka-split
            if " aka " in at.lower():
                for part in re.split(r'\s+aka\s+', at, flags=re.IGNORECASE):
                    p = part.strip()
                    if p:
                        candidates.append(p)

    for c in candidates:
        cn = _normalize_key(c)
        # Exakter Match oder einer ist Substring des anderen
        if q == cn:
            return True
        if len(q) >= 4 and (q in cn or cn in q):
            return True
        # Fuzzy: mindestens 80% gleiche Zeichen
        if len(q) >= 4 and len(cn) >= 4:
            shorter = min(len(q), len(cn))
            longer = max(len(q), len(cn))
            if shorter / longer >= 0.75 and (q[:shorter] == cn[:shorter]):
                return True
    return False


def _collect_titles_from_entry(entry: dict) -> set[str]:
    """Sammelt alle Titel-Varianten aus einem Sonarr/Radarr Lookup-Ergebnis."""
    tr_map = {"İ": "I", "ı": "i", "Ş": "S", "ş": "s", "Ğ": "G", "ğ": "g",
              "Ü": "U", "ü": "u", "Ö": "O", "ö": "o", "Ç": "C", "ç": "c"}
    titles = set()

    for field in ["title", "originalTitle"]:
        val = entry.get(field, "") or ""
        if val:
            titles.add(val)
            stripped = val
            for k, v in tr_map.items():
                stripped = stripped.replace(k, v)
            if stripped != val:
                titles.add(stripped)

    for a in entry.get("alternateTitles", []):
        alt = a.get("title", "")
        if not alt:
            continue
        titles.add(alt)
        # aka-Split
        if " aka " in alt.lower():
            for part in re.split(r'\s+aka\s+', alt, flags=re.IGNORECASE):
                p = part.strip()
                if p and len(p) >= 2:
                    titles.add(p)
        # Bindestrich-Split
        if " - " in alt:
            for part in alt.split(" - "):
                p = part.strip()
                if p and len(p) >= 3:
                    titles.add(p)
        # ASCII-Variante
        stripped = alt
        for k, v in tr_map.items():
            stripped = stripped.replace(k, v)
        if stripped != alt:
            titles.add(stripped)

    titles.discard("")
    return titles


def lookup_tvdb_titles(query: str) -> list[str]:
    """
    Sucht über Sonarr UND Radarr Lookup nach einem Titel.
    Gibt nur Alternativtitel von **relevanten** Treffern zurück.
    """
    all_titles = set()

    # ── Sonarr Lookup (Serien) ──
    try:
        resp = requests.get(
            f"{SONARR_URL}/api/v3/series/lookup",
            params={"apikey": SONARR_API_KEY, "term": query},
            timeout=10
        )
        if resp.ok:
            for entry in resp.json()[:5]:
                if _is_relevant(query, entry):
                    all_titles |= _collect_titles_from_entry(entry)
                    # Wenn wir einen relevanten Treffer haben, versuche TVDB-ID Lookup
                    # für mehr Alternativtitel (Sonarr gibt bei ID-Suche manchmal mehr zurück)
                    tvdb_id = entry.get("tvdbId")
                    if tvdb_id:
                        try:
                            r2 = requests.get(
                                f"{SONARR_URL}/api/v3/series/lookup",
                                params={"apikey": SONARR_API_KEY, "term": f"tvdb:{tvdb_id}"},
                                timeout=8
                            )
                            if r2.ok:
                                for e2 in r2.json()[:1]:
                                    all_titles |= _collect_titles_from_entry(e2)
                        except Exception:
                            pass
                    break  # Nur den besten relevanten Treffer nehmen
    except Exception:
        pass

    # ── Radarr Lookup (Filme) ──
    try:
        resp = requests.get(
            f"{RADARR_URL}/api/v3/movie/lookup",
            params={"apikey": RADARR_API_KEY, "term": query},
            timeout=10
        )
        if resp.ok:
            results = resp.json()[:5]
            # Erst strengen Match versuchen, dann ersten Treffer nehmen
            # (Radarr-Lookup liefert bereits nach Relevanz sortiert)
            best = None
            for entry in results:
                if _is_relevant(query, entry):
                    best = entry
                    break
            if best is None and len(results) == 1:
                # Wenn nur ein Ergebnis: nehmen wir es (Radarr hat bereits gefiltert)
                best = results[0]
            if best:
                all_titles |= _collect_titles_from_entry(best)
                # Auch über tmdbId nochmal nachschlagen – Radarr gibt dort
                # oft mehr Alternativtitel zurück (inkl. englischer Titel)
                tmdb_id = best.get("tmdbId")
                if tmdb_id:
                    try:
                        r2 = requests.get(
                            f"{RADARR_URL}/api/v3/movie/lookup",
                            params={"apikey": RADARR_API_KEY, "term": f"tmdb:{tmdb_id}"},
                            timeout=8
                        )
                        if r2.ok:
                            for e2 in r2.json()[:1]:
                                all_titles |= _collect_titles_from_entry(e2)
                    except Exception:
                        pass
    except Exception:
        pass

    all_titles.discard("")
    return sorted(all_titles)


# Lern-DB beim Start laden
_load_learned_db()

# ============================================================
# Logging Setup
# ============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TurkARRBridge")

# ============================================================
# ARR Indexer Self-Healing
# ============================================================

_arr_heal_thread: Optional[threading.Thread] = None
_arr_heal_event = threading.Event()
_arr_heal_lock = threading.Lock()
_arr_heal_status_lock = threading.Lock()
_arr_heal_status = {
    "enabled": bool(_config.get("arr_indexer_auto_heal", True)),
    "last_check": "",
    "upstream_ok": None,
    "tested": 0,
    "recovered": 0,
    "error": "",
}


def _arr_api_headers(api_key: str) -> dict:
    """Standard-Header für Sonarr/Radarr v3, ohne Key in URLs/Logs."""
    return {"X-Api-Key": api_key, "Accept": "application/json"}


def _arr_indexer_field(indexer: dict, name: str, default=""):
    """Liest ein Feld aus dem von Sonarr/Radarr gelieferten Indexer-Modell."""
    for field in indexer.get("fields", []):
        if str(field.get("name", "")).lower() == name.lower():
            return field.get("value", default)
    return default


def _is_this_bridge_indexer(indexer: dict) -> bool:
    """Erkennt ausschließlich Torznab-Indexer, die auf diese Bridge zeigen."""
    implementation = " ".join(
        str(indexer.get(key, ""))
        for key in ("implementation", "implementationName", "configContract")
    ).lower()
    if "torznab" not in implementation:
        return False

    base_url = str(_arr_indexer_field(indexer, "baseUrl", "") or "").strip()
    if not base_url:
        return False
    try:
        parsed = urllib.parse.urlparse(base_url)
        effective_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        bridge_port = int(_config.get("bridge_port", BRIDGE_PORT) or BRIDGE_PORT)
    except (TypeError, ValueError):
        return False

    external_url = str(_config.get("bridge_external_url", "") or "").strip()
    if external_url:
        try:
            external = urllib.parse.urlparse(external_url)
            if parsed.netloc.lower() == external.netloc.lower():
                return True
        except ValueError:
            pass

    # Der Host kann je nach Docker-/NAS-Netz anders heißen. Port plus der
    # Bridge-spezifische Torznab-Pfad sind stabiler als ein Hostvergleich.
    api_path = str(_arr_indexer_field(indexer, "apiPath", "") or "").lower()
    path = (parsed.path.rstrip("/") + "/" + api_path.lstrip("/")).lower()
    return effective_port == bridge_port and "torznab" in path


def _probe_upstream_torznab() -> tuple[bool, str]:
    """Echte No-Match-Suche: prüft Jackett, Tracker-Login und Parser gemeinsam."""
    if not UPSTREAM_TORZNAB_URL or not JACKETT_API_KEY:
        return False, "Upstream-Torznab oder Jackett API-Key fehlt"
    try:
        response = requests.get(
            UPSTREAM_TORZNAB_URL,
            params={
                "apikey": JACKETT_API_KEY,
                "t": "search",
                "q": "TurkARRBridgeHealthProbeNoMatch",
                "limit": 1,
            },
            timeout=45,
        )
        if not response.ok:
            return False, f"Jackett HTTP {response.status_code}"
        try:
            root = etree.fromstring(response.content)
            if etree.QName(root).localname.lower() == "error":
                return False, root.get("description") or "Torznab-Fehler"
        except (etree.XMLSyntaxError, ValueError):
            return False, "Jackett lieferte kein gültiges Torznab-XML"
        return True, ""
    except requests.RequestException as exc:
        return False, f"Jackett nicht erreichbar: {type(exc).__name__}"


def _arr_has_indexer_warning(base_url: str, api_key: str) -> bool:
    """Erkennt Sonarr/Radarr-Warnungen über gesperrte Indexer."""
    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/api/v3/health",
            headers=_arr_api_headers(api_key),
            timeout=10,
        )
        if not response.ok:
            return False
        for warning in response.json():
            source = str(warning.get("source", "")).lower()
            message = str(warning.get("message", "")).lower()
            if "indexer" in source or "indexer" in message:
                return True
    except (requests.RequestException, ValueError, TypeError):
        pass
    return False


def heal_arr_indexers() -> dict:
    """Reaktiviert nur diese Bridge, nachdem der Tracker real verifiziert wurde.

    Sonarr und Radarr behalten Indexerfehler über Neustarts hinweg. Nach einer
    Tracker-/Cookie-Reparatur fragen sie den inzwischen gesunden Indexer deshalb
    oft weiterhin nicht ab. Ein erfolgreicher offizieller Indexer-Test setzt
    genau diesen Fehlerzustand zurück, ohne Konfigurationen zu verändern.
    """
    enabled = bool(_config.get("arr_indexer_auto_heal", True))
    result = {
        "enabled": enabled,
        "last_check": datetime.now().isoformat(timespec="seconds"),
        "upstream_ok": False,
        "tested": 0,
        "recovered": 0,
        "error": "",
    }
    if not enabled:
        with _arr_heal_status_lock:
            _arr_heal_status.update(result)
        return result

    if not _arr_heal_lock.acquire(blocking=False):
        result["error"] = "Prüfung läuft bereits"
        return result

    try:
        upstream_ok, upstream_error = _probe_upstream_torznab()
        result["upstream_ok"] = upstream_ok
        if not upstream_ok:
            result["error"] = upstream_error
            logger.warning(f"[ARR-HEAL] Kein Reset: {upstream_error}")
            return result

        service_errors = []
        for service_name, base_url, api_key in (
            ("Sonarr", SONARR_URL, SONARR_API_KEY),
            ("Radarr", RADARR_URL, RADARR_API_KEY),
        ):
            if not base_url or not api_key:
                continue
            headers = _arr_api_headers(api_key)
            had_warning = _arr_has_indexer_warning(base_url, api_key)
            try:
                response = requests.get(
                    f"{base_url.rstrip('/')}/api/v3/indexer",
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                indexers = response.json()
            except (requests.RequestException, ValueError, TypeError) as exc:
                service_errors.append(f"{service_name}: {type(exc).__name__}")
                continue

            for indexer in indexers:
                if not _is_this_bridge_indexer(indexer):
                    continue
                if not any(indexer.get(flag, False) for flag in (
                    "enableRss", "enableAutomaticSearch", "enableInteractiveSearch"
                )):
                    continue
                try:
                    test_response = requests.post(
                        f"{base_url.rstrip('/')}/api/v3/indexer/test",
                        headers={**headers, "Content-Type": "application/json"},
                        json=indexer,
                        timeout=60,
                    )
                    result["tested"] += 1
                    if test_response.ok and had_warning:
                        result["recovered"] += 1
                        logger.info(
                            f"[ARR-HEAL] {service_name}-Indexer "
                            f"'{indexer.get('name', indexer.get('id', '?'))}' reaktiviert"
                        )
                    elif not test_response.ok:
                        service_errors.append(
                            f"{service_name} Test HTTP {test_response.status_code}"
                        )
                except requests.RequestException as exc:
                    service_errors.append(f"{service_name} Test: {type(exc).__name__}")

        result["error"] = "; ".join(service_errors)
        if result["tested"] == 0 and not result["error"]:
            result["error"] = "Kein Bridge-Torznab-Indexer in Sonarr/Radarr gefunden"
        return result
    finally:
        with _arr_heal_status_lock:
            _arr_heal_status.update(result)
        _arr_heal_lock.release()


def _arr_heal_loop():
    """Prüft beim Start und danach periodisch auf persistente ARR-Sperren."""
    if _arr_heal_event.wait(20):
        _arr_heal_event.clear()
    while True:
        try:
            heal_arr_indexers()
        except Exception as exc:
            logger.error(f"[ARR-HEAL] Unerwarteter Fehler: {exc}")
        try:
            minutes = max(
                5, int(_config.get("arr_indexer_heal_interval_minutes", 15) or 15)
            )
        except (TypeError, ValueError):
            minutes = 15
        _arr_heal_event.wait(minutes * 60)
        _arr_heal_event.clear()


def _start_arr_heal_thread():
    global _arr_heal_thread
    if _arr_heal_thread is None or not _arr_heal_thread.is_alive():
        _arr_heal_thread = threading.Thread(target=_arr_heal_loop, daemon=True)
        _arr_heal_thread.start()
        logger.info("[ARR-HEAL] Auto-Healing gestartet")


def _request_arr_indexer_heal():
    """Weckt den Healer direkt nach einer erfolgreichen Tracker-Reparatur."""
    _arr_heal_event.set()

# ============================================================
# Türkische Zeichen Mapping
# ============================================================

TURKISH_CHAR_MAP = {
    "İ": "I", "ı": "i",
    "Ş": "S", "ş": "s",
    "Ğ": "G", "ğ": "g",
    "Ü": "U", "ü": "u",
    "Ö": "O", "ö": "o",
    "Ç": "C", "ç": "c",
    "â": "a", "Â": "A",
    "î": "i", "Î": "I",
    "û": "u", "Û": "U",
}


def strip_turkish_chars(text: str) -> str:
    """Ersetzt türkische Sonderzeichen durch ASCII-Äquivalente."""
    result = text
    for tr_char, ascii_char in TURKISH_CHAR_MAP.items():
        result = result.replace(tr_char, ascii_char)
    return result


def normalize_for_search(text: str) -> str:
    """Normalisiert einen Titel für die Suche: Kleinbuchstaben, keine Sonderzeichen."""
    text = strip_turkish_chars(text).lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def release_matches_search(title: str, search_titles: list[str]) -> bool:
    """Prueft einen Release-Titel vor einem automatischen Download streng."""
    def _words(value: str) -> list[str]:
        # Release-Namen verwenden Punkte/Bindestriche als Leerzeichen. Die
        # normale Cache-Normalisierung entfernt sie dagegen absichtlich.
        value = strip_turkish_chars(value).lower()
        return re.sub(r'[^a-z0-9]+', ' ', value).strip().split()

    release_words = _words(title)
    if not release_words:
        return False

    for search_title in search_titles:
        # Das Suchjahr ist kein Bestandteil des eigentlichen Serientitels.
        candidate = re.sub(r'\s+\d{4}$', '', search_title or '').strip()
        wanted_words = _words(candidate)
        # Sehr kurze Varianten sind fuer einen sicheren Auto-Download zu vage.
        if not wanted_words or len(''.join(wanted_words)) < 4:
            continue
        width = len(wanted_words)
        # Kurze Aliase duerfen verwandte Spin-offs zwar in Jackett finden,
        # aber nur als Identitaet gelten, wenn der Release damit beginnt.
        if width <= 2:
            matched = release_words[:width] == wanted_words
        else:
            matched = any(release_words[i:i + width] == wanted_words
                          for i in range(len(release_words) - width + 1))
        if matched:
            return True
    return False


def multi_release_covers_season(title: str, season: int) -> bool:
    """Prueft, ob ein Multi-Season-Release die angefragte Staffel abdeckt."""
    ranges = re.findall(r'\bS(\d{1,2})\s*-\s*S?(\d{1,2})\b', title, re.I)
    if ranges:
        return any(min(int(start), int(end)) <= season <= max(int(start), int(end))
                   for start, end in ranges)

    # Bei explizit als komplett bezeichneten Paketen ist keine Spanne noetig.
    return bool(re.search(
        r'box\s*set|t[\u00fcu]m\s*sezon|t[\u00fcu]m\s*b[\u00f6o]l[\u00fcu]m|'
        r'b[\u00fcu]t[\u00fcu]n\s*b[\u00f6o]l[\u00fcu]m|komple|complete\s*series|all\s*seasons',
        title, re.I
    ))


def release_episode_for_season(title: str, season: int) -> Optional[int]:
    """Liest SxxExx aus einem Release und begrenzt es auf die gesuchte Staffel."""
    match = re.search(r'\bS(\d{1,2})\s*E(\d{1,3})\b', title, re.I)
    if match and int(match.group(1)) == season:
        return int(match.group(2))
    return None


# ============================================================
# Titel-Mapping Cache
# ============================================================

class TitleCache:
    """
    Cache für Titel-Mappings: Internationaler Titel → Türkischer Originaltitel.
    Wird aus Sonarr und Radarr befüllt.
    """

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._cache: dict = {}  # normalized_title → {titles: [...], updated: datetime}
        self._tvdb_titles: dict[str, str] = {}  # TVDB-ID → Sonarr-Haupttitel
        self._last_refresh: Optional[datetime] = None

    def _cache_key(self, title: str) -> str:
        return normalize_for_search(title)

    def add_mapping(self, international_title: str, alternate_titles: list[str],
                    original_title: str = "", tvdb_id=None):
        """Fügt ein Titel-Mapping hinzu."""
        key = self._cache_key(international_title)
        all_titles = set()

        # Internationaler Titel selbst
        all_titles.add(international_title)
        # Auch der Haupttitel kann türkische Zeichen enthalten. Viele Indexer
        # speichern Releases ausschließlich in ASCII (Doğu -> Dogu), während
        # Sonarr den korrekten Unicode-Titel sendet. Daher immer beide Varianten.
        ascii_international = strip_turkish_chars(international_title)
        if ascii_international != international_title:
            all_titles.add(ascii_international)

        # Lange internationale Titel enthalten oft einen Untertitel, waehrend
        # Tracker nur den kurzen Haupttitel fuehren ("Titel: The Story ...").
        # Nur eindeutige Trenner verwenden, um beliebige Wortkuerzungen zu meiden.
        for separator in (":", " - "):
            if separator in international_title:
                short_title = international_title.split(separator, 1)[0].strip(" .-")
                if len(normalize_for_search(short_title)) >= 4:
                    all_titles.add(short_title)
                    all_titles.add(strip_turkish_chars(short_title))

        # Originaltitel
        if original_title:
            all_titles.add(original_title)
            all_titles.add(strip_turkish_chars(original_title))

        # Alle Alternativtitel
        for alt in alternate_titles:
            all_titles.add(alt)
            all_titles.add(strip_turkish_chars(alt))
            # "aka"-Titel aufsplitten: "Masum aka Innocent" → "Masum", "Innocent"
            if " aka " in alt.lower():
                parts = re.split(r'\s+aka\s+', alt, flags=re.IGNORECASE)
                for part in parts:
                    part = part.strip()
                    if part and len(part) >= 2:
                        all_titles.add(part)
                        all_titles.add(strip_turkish_chars(part))
            # Auch Titel mit " - " aufsplitten: "Foo - Bar" → "Foo", "Bar"
            if " - " in alt:
                parts = alt.split(" - ")
                for part in parts:
                    part = part.strip()
                    if part and len(part) >= 3:
                        all_titles.add(part)
                        all_titles.add(strip_turkish_chars(part))

        # Punkte und Doppelpunkte werden von Tracker-Suchfeldern teils als
        # echte Zeichen behandelt. Wortgleiche, bereinigte Varianten helfen,
        # ohne die Titelidentitaet zu verkuerzen.
        for known_title in list(all_titles):
            cleaned = re.sub(r'[^\w]+', ' ', known_title, flags=re.UNICODE)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if cleaned and cleaned != known_title:
                all_titles.add(cleaned)
                all_titles.add(strip_turkish_chars(cleaned))

        # Tuerkische Tracker verwenden Zahlwoerter und Ziffern wechselnd
        # ("Yedi Numara" / "7 Numara"). Beide Formen als Suchalias fuehren.
        number_words = {
            "sifir": "0", "sıfır": "0", "bir": "1", "iki": "2",
            "uc": "3", "üç": "3", "dort": "4", "dört": "4",
            "bes": "5", "beş": "5", "alti": "6", "altı": "6",
            "yedi": "7", "sekiz": "8", "dokuz": "9", "on": "10",
        }
        for known_title in list(all_titles):
            words = known_title.split()
            replaced = [number_words.get(word.casefold(), word) for word in words]
            numeric_title = " ".join(replaced)
            if numeric_title != known_title:
                all_titles.add(numeric_title)
                all_titles.add(strip_turkish_chars(numeric_title))

        # Auch für jeden Alias als Key registrieren
        for title in list(all_titles):
            alt_key = self._cache_key(title)
            if alt_key not in self._cache:
                self._cache[alt_key] = {"titles": set(), "updated": datetime.now()}
            self._cache[alt_key]["titles"].update(all_titles)

        if key not in self._cache:
            self._cache[key] = {"titles": set(), "updated": datetime.now()}
        self._cache[key]["titles"].update(all_titles)
        self._cache[key]["updated"] = datetime.now()

        if tvdb_id:
            self._tvdb_titles[str(tvdb_id)] = international_title

    def title_for_tvdb_id(self, tvdb_id) -> str:
        """Liefert den Sonarr-Titel zu einer Torznab-TVDB-ID."""
        return self._tvdb_titles.get(str(tvdb_id or ""), "")

    def get_search_titles(self, query: str) -> list[str]:
        """
        Gibt alle bekannten Titel für einen Suchbegriff zurück.
        Falls kein Mapping gefunden: gibt den Originalbegriff + ASCII-Variante zurück.
        """
        key = self._cache_key(query)
        if key in self._cache:
            entry = self._cache[key]
            titles = list(entry["titles"])
            logger.info(f"Cache-Hit für '{query}': {len(titles)} Varianten gefunden")
            return titles

        # Jahreszahl am Ende entfernen und nochmal suchen (z.B. "Government Woman 2 2013" → "Government Woman 2")
        key_no_year = re.sub(r'\s*\d{4}$', '', key).strip()
        if key_no_year != key and key_no_year in self._cache:
            entry = self._cache[key_no_year]
            titles = list(entry["titles"])
            logger.info(f"Cache-Hit (ohne Jahr) für '{query}': {len(titles)} Varianten gefunden")
            return titles

        # Auch in der Lern-DB ohne Jahreszahl suchen
        if key_no_year != key:
            with _learned_db_lock:
                learned_entry = _learned_db.get(key_no_year)
            if learned_entry:
                titles = learned_entry.get("titles", [])
                if titles:
                    logger.info(f"Lern-DB-Hit (ohne Jahr) für '{query}': {len(titles)} Varianten")
                    self._cache[key] = {"titles": set(titles), "tvdb_id": None}
                    return list(titles)

        # Kein exakter Match - versuche Teilmatch (nur wenn der Key lang genug ist)
        if len(key) >= 5:
            best_match = None
            best_score = 0
            for cached_key, entry in self._cache.items():
                if key == cached_key:
                    continue
                shorter = min(len(key), len(cached_key))
                longer = max(len(key), len(cached_key))
                # Mindestens 75% Längenverhältnis
                if shorter / longer < 0.75:
                    continue
                # Wort-basierter Subset-Match: Jahreszahl ignorieren beim Vergleich
                key_words = set(key.split()) - {'2013','2014','2015','2016','2017','2018','2019','2020','2021','2022','2023','2024','2025','2026'}
                cached_words = set(cached_key.split()) - {'2013','2014','2015','2016','2017','2018','2019','2020','2021','2022','2023','2024','2025','2026'}
                if key_words.issubset(cached_words) or cached_words.issubset(key_words):
                    score = shorter / longer
                    if score > best_score:
                        best_score = score
                        best_match = entry
            if best_match:
                titles = list(best_match["titles"])
                logger.info(f"Cache-Partial-Hit für '{query}': {len(titles)} Varianten (score: {best_score:.2f})")
                return titles

        # ── Cache-Miss: Zuerst in der gelernten Datenbank nachschlagen ──
        with _learned_db_lock:
            learned_entry = _learned_db.get(key)
        if learned_entry:
            titles = learned_entry.get("titles", [])
            if titles:
                logger.info(f"Lern-DB-Hit für '{query}': {len(titles)} Varianten")
                # Auch in den In-Memory-Cache laden damit es schneller wird
                self._cache[key] = {"titles": set(titles), "tvdb_id": None}
                # Suchzähler erhöhen
                def _inc():
                    with _learned_db_lock:
                        if key in _learned_db:
                            _learned_db[key]["search_count"] = _learned_db[key].get("search_count", 0) + 1
                    _save_learned_db()
                threading.Thread(target=_inc, daemon=True).start()
                return list(titles)

        # ── Echter Cache-Miss: TVDB-Lookup im Hintergrund ──
        stripped = strip_turkish_chars(query)
        result = [query]
        if stripped != query:
            result.append(stripped)
        logger.info(f"Cache-Miss für '{query}': Fallback mit {len(result)} Varianten – starte TVDB-Lookup")

        # Asynchroner TVDB-Lookup, damit die Suchanfrage nicht blockiert wird
        def _async_learn(q: str, k: str):
            try:
                discovered = lookup_tvdb_titles(q)
                if discovered and len(discovered) >= 2:
                    is_new = _add_learned(q, discovered, source="tvdb")
                    if is_new:
                        logger.info(f"[LEARN] '{q}' → {len(discovered)} Varianten gespeichert: {discovered[:5]}")
                        # Nur in In-Memory-Cache aufnehmen wenn dort KEIN
                        # vorhandener Bibliotheks-Eintrag existiert (Bibliothek hat Vorrang)
                        if k not in self._cache:
                            self._cache[k] = {"titles": set(discovered), "tvdb_id": None}
                        else:
                            # Ergänze vorhandene Titel statt zu überschreiben
                            self._cache[k]["titles"] |= set(discovered)
            except Exception as e:
                logger.debug(f"[LEARN] Lookup-Fehler für '{q}': {e}")

        threading.Thread(target=_async_learn, args=(query, key), daemon=True).start()
        return result

    def is_stale(self) -> bool:
        if self._last_refresh is None:
            return True
        return (datetime.now() - self._last_refresh).total_seconds() > self.ttl

    def mark_refreshed(self):
        self._last_refresh = datetime.now()

    def stats(self) -> dict:
        return {
            "entries": len(self._cache),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "stale": self.is_stale()
        }


# ============================================================
# ARR API Integration
# ============================================================

def fetch_sonarr_series(url: str, api_key: str) -> list[dict]:
    """Holt alle Serien aus Sonarr mit ihren alternativen Titeln."""
    try:
        resp = requests.get(
            f"{url}/api/v3/series",
            params={"apikey": api_key},
            timeout=30
        )
        resp.raise_for_status()
        series = resp.json()
        logger.info(f"Sonarr: {len(series)} Serien geladen")
        return series
    except Exception as e:
        logger.error(f"Sonarr API Fehler: {e}")
        return []


def fetch_radarr_movies(url: str, api_key: str) -> list[dict]:
    """Holt alle Filme aus Radarr mit ihren alternativen Titeln."""
    try:
        resp = requests.get(
            f"{url}/api/v3/movie",
            params={"apikey": api_key},
            timeout=30
        )
        resp.raise_for_status()
        movies = resp.json()
        logger.info(f"Radarr: {len(movies)} Filme geladen")
        return movies
    except Exception as e:
        logger.error(f"Radarr API Fehler: {e}")
        return []


def fetch_wikidata_titles_by_imdb(imdb_ids: list[str]) -> dict[str, set[str]]:
    """Loest IMDb-IDs gesammelt in Original-/tuerkische Titel auf.

    Sonarr/TVDB liefern bei vielen nicht-englischen Serien keinen Originaltitel.
    Wikidata verbindet die bereits vorhandene IMDb-ID mit dem Originaltitel,
    ohne dass serienbezogene Mappings gepflegt werden muessen.
    """
    valid_ids = sorted({value for value in imdb_ids
                        if re.fullmatch(r"tt\d+", value or "")})
    if not valid_ids:
        return {}

    values = " ".join(f'"{value}"' for value in valid_ids)
    sparql = f"""
        SELECT ?imdb ?itemLabel ?nativeTitle WHERE {{
          VALUES ?imdb {{ {values} }}
          ?item wdt:P345 ?imdb.
          OPTIONAL {{ ?item wdt:P1476 ?nativeTitle. }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "tr,en". }}
        }}
    """
    try:
        resp = requests.get(
            "https://query.wikidata.org/sparql",
            params={"query": sparql, "format": "json"},
            headers={"User-Agent": "TurkARRBridge/1.0"},
            timeout=25,
        )
        resp.raise_for_status()
        mappings: dict[str, set[str]] = {}
        for binding in resp.json().get("results", {}).get("bindings", []):
            imdb_id = binding.get("imdb", {}).get("value", "")
            if not imdb_id:
                continue
            titles = mappings.setdefault(imdb_id, set())
            for field in ("itemLabel", "nativeTitle"):
                title = binding.get(field, {}).get("value", "").strip()
                if title and not title.startswith("http"):
                    titles.add(title)
        logger.info(
            f"Wikidata: Originaltitel fuer {len(mappings)}/{len(valid_ids)} "
            "IMDb-IDs geladen"
        )
        return mappings
    except Exception as e:
        logger.warning(f"Wikidata-Titelabfrage fehlgeschlagen: {e}")
        return {}


def _load_learned_into_cache(cache: TitleCache):
    """Lädt alle persistent gelernten Mappings in den In-Memory-Cache."""
    with _learned_db_lock:
        snapshot = dict(_learned_db)
    count = 0
    for key, entry in snapshot.items():
        titles = entry.get("titles", [])
        if titles and len(titles) >= 2:
            primary = titles[0]
            alts = titles[1:]
            cache.add_mapping(primary, alts, "")
            count += 1
    if count:
        logger.info(f"[LEARN] {count} gelernte Mappings in Cache geladen")


def refresh_title_cache(cache: TitleCache):
    """Aktualisiert den Titel-Cache aus Sonarr, Radarr und der Lern-DB."""
    if not cache.is_stale():
        return

    logger.info("Aktualisiere Titel-Cache aus Sonarr & Radarr...")

    # Sonarr Serien
    series = fetch_sonarr_series(SONARR_URL, SONARR_API_KEY)
    wikidata_titles = fetch_wikidata_titles_by_imdb(
        [s.get("imdbId", "") for s in series]
    )
    for s in series:
        title = s.get("title", "")
        alts = [a.get("title", "") for a in s.get("alternateTitles", []) if a.get("title")]
        alts.extend(sorted(wikidata_titles.get(s.get("imdbId", ""), set())))
        original = s.get("originalTitle", "") or ""
        if title:
            cache.add_mapping(title, alts, original, tvdb_id=s.get("tvdbId"))

    # Radarr Filme
    movies = fetch_radarr_movies(RADARR_URL, RADARR_API_KEY)
    for m in movies:
        title = m.get("title", "")
        alts = [a.get("title", "") for a in m.get("alternateTitles", []) if a.get("title")]
        original = m.get("originalTitle", "") or ""
        if title:
            cache.add_mapping(title, alts, original)

    # Gelernte Mappings (persistente Lern-DB) immer mit einlesen
    _load_learned_into_cache(cache)

    cache.mark_refreshed()
    stats = cache.stats()
    learned_count = len(_learned_db)
    logger.info(f"Titel-Cache aktualisiert: {stats['entries']} Einträge (davon {learned_count} gelernt)")


# ============================================================
# Torznab XML Merging
# ============================================================

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
ATOM_NS = "http://www.w3.org/2005/Atom"


# ============================================================
# qBittorrent Integration (für BoxSet Auto-Download)
# ============================================================

_qbit_session: Optional[requests.Session] = None
_qbit_session_key: tuple[str, str] = ("", "")
_qbit_session_time: float = 0
_qbit_session_lock = threading.RLock()


def _qbit_verify_session(session: requests.Session, base_url: str,
                         timeout: int = 8) -> tuple[bool, str, str]:
    """Verifiziert die Anmeldung ueber einen geschuetzten WebAPI-Endpunkt."""
    try:
        response = session.get(
            f"{base_url}/api/v2/app/version",
            timeout=timeout,
            allow_redirects=False,
        )
        version = response.text.strip()
        if response.ok and version:
            return True, version, ""
        return False, "", f"WebAPI-Prüfung HTTP {response.status_code}"
    except requests.RequestException as exc:
        return False, "", f"WebAPI nicht erreichbar: {type(exc).__name__}"


def _qbit_invalidate_session(session: Optional[requests.Session] = None):
    """Verwirft einen abgelaufenen oder durch ein Update ungueltigen Login."""
    global _qbit_session, _qbit_session_key, _qbit_session_time
    with _qbit_session_lock:
        if session is not None and session is not _qbit_session:
            return
        if _qbit_session is not None:
            try:
                _qbit_session.close()
            except Exception:
                pass
        _qbit_session = None
        _qbit_session_key = ("", "")
        _qbit_session_time = 0


def qbit_connect(qbit_url: Optional[str] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 force_login: bool = False
                 ) -> tuple[Optional[requests.Session], str, str]:
    """Oeffnet eine versionsunabhaengige qBittorrent-WebAPI-Session.

    Seit qBittorrent 5.2 koennen erfolgreiche, inhaltslose API-Antworten HTTP
    204 verwenden und das Session-Cookie enthaelt den WebUI-Port im Namen.
    Darum wertet die Bridge weder einen festen Response-Text noch einen festen
    Cookie-Namen aus. Entscheidend ist ausschliesslich, ob ein anschliessender
    authentifizierter WebAPI-Aufruf funktioniert.
    """
    global _qbit_session, _qbit_session_key, _qbit_session_time

    base_url = (QBIT_URL if qbit_url is None else qbit_url).strip().rstrip("/")
    user = QBIT_USER if username is None else username
    secret = QBIT_PASS if password is None else password
    session_key = (base_url, user)

    if not base_url:
        return None, "", "qBittorrent URL nicht konfiguriert"

    with _qbit_session_lock:
        if (not force_login and _qbit_session is not None
                and _qbit_session_key == session_key
                and time.time() - _qbit_session_time < 1800):
            ok, version, error = _qbit_verify_session(_qbit_session, base_url)
            if ok:
                return _qbit_session, version, ""
            logger.info(f"[QBIT] Gespeicherte Session ungültig: {error}")
            _qbit_invalidate_session(_qbit_session)

        session = requests.Session()
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            # Von qBittorrent fuer Host-/CSRF-Pruefungen empfohlen.
            session.headers.update({"Origin": origin, "Referer": origin + "/"})

        try:
            login_response = session.post(
                f"{base_url}/api/v2/auth/login",
                data={"username": user, "password": secret},
                timeout=12,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            session.close()
            return None, "", f"Login nicht erreichbar: {type(exc).__name__}"

        login_body = login_response.text.strip().casefold()
        if (not login_response.ok
                or login_body in ("fails.", "fail.", "forbidden")):
            error = f"Login fehlgeschlagen (HTTP {login_response.status_code})"
            session.close()
            return None, "", error

        ok, version, error = _qbit_verify_session(session, base_url)
        if not ok:
            session.close()
            return None, "", error or "Login konnte nicht bestätigt werden"

        _qbit_invalidate_session()
        _qbit_session = session
        _qbit_session_key = session_key
        _qbit_session_time = time.time()
        logger.info(f"[QBIT] Login erfolgreich, qBittorrent {version}")
        return session, version, ""


def qbit_login() -> Optional[requests.Session]:
    """Kompatibilitaets-Wrapper fuer den produktiven qBittorrent-Login."""
    session, _version, error = qbit_connect()
    if session is None:
        logger.error(f"[QBIT] Login fehlgeschlagen: {error}")
    return session


def qbit_add_torrent(torrent_url: str, category: str = "tv-tr-boxset") -> bool:
    """Fügt einen Torrent (per URL) in qBittorrent hinzu. Download-Pfad wird von qBit bestimmt."""
    session = qbit_login()
    if session is None:
        return False
    try:
        base_url = QBIT_URL.strip().rstrip("/")
        files = None
        data = {"category": category, "paused": "false"}

        if torrent_url.lower().startswith("magnet:"):
            data["urls"] = torrent_url
        else:
            # Private Jackett-Links zuerst mit Jacketts Berechtigung laden und
            # anschliessend als Datei an qBittorrent uebertragen.
            dl_resp = requests.get(torrent_url, timeout=30)
            if not dl_resp.ok:
                logger.error(
                    f"[QBIT] Torrent-Download fehlgeschlagen: {dl_resp.status_code}"
                )
                return False

            content_type = dl_resp.headers.get("Content-Type", "").lower()
            is_torrent_file = (
                dl_resp.content.startswith(b"d")
                or "bittorrent" in content_type
                or "octet-stream" in content_type
            )
            if not is_torrent_file:
                logger.error(
                    f"[QBIT] Jackett lieferte keine Torrent-Datei "
                    f"(Content-Type: {content_type or 'unbekannt'})"
                )
                return False
            files = {
                "torrents": (
                    "boxset.torrent",
                    dl_resp.content,
                    "application/x-bittorrent",
                )
            }

        def _add(active_session: requests.Session):
            return active_session.post(
                f"{base_url}/api/v2/torrents/add",
                files=files,
                data=data,
                timeout=20,
                allow_redirects=False,
            )

        response = _add(session)
        if response.status_code in (401, 403):
            # Ein qBittorrent-Restart invalidiert gecachte Sessions. Genau
            # einmal neu anmelden und den Add-Aufruf wiederholen.
            _qbit_invalidate_session(session)
            session, _version, error = qbit_connect(force_login=True)
            if session is None:
                logger.error(f"[QBIT] Re-Login fehlgeschlagen: {error}")
                return False
            response = _add(session)

        response_body = response.text.strip().casefold()
        if response.ok and (not response_body or response_body.startswith("ok")):
            logger.info(
                f"[QBIT] Torrent erfolgreich hinzugefügt (Kategorie: {category})"
            )
            return True
        logger.error(
            f"[QBIT] Add fehlgeschlagen: {response.status_code} "
            f"{response.text[:100]}"
        )
    except Exception as e:
        logger.error(f"[QBIT] Fehler: {e}")
    return False


# ============================================================
# Telegram-Benachrichtigungen
# ============================================================

def send_telegram(message: str, parse_mode: str = "HTML"):
    """Sendet eine Telegram-Nachricht."""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": parse_mode,
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"[TELEGRAM] Fehler: {e}")


class UpstreamTorznabError(RuntimeError):
    """Strukturierter Upstream-Fehler, den die Bridge an ARR melden kann."""

    def __init__(self, message: str, body: bytes = b"",
                 content_type: str = "application/xml; charset=UTF-8"):
        super().__init__(message)
        self.body = body
        self.content_type = content_type


def _torznab_error_xml(message: str) -> bytes:
    error = etree.Element("error", code="900", description=message[:500])
    return etree.tostring(error, xml_declaration=True, encoding="UTF-8")


def fetch_torznab(query: str, params: dict) -> Optional[bytes]:
    """Führt eine Torznab-Suche beim Upstream-Indexer durch."""
    upstream_params = dict(params)
    upstream_params["q"] = query
    upstream_params["apikey"] = JACKETT_API_KEY

    try:
        resp = requests.get(UPSTREAM_TORZNAB_URL, params=upstream_params, timeout=60)
        if not resp.ok:
            message = f"Jackett HTTP {resp.status_code}"
            try:
                error_doc = etree.fromstring(resp.content)
                description = error_doc.get("description")
                if description:
                    message = description.splitlines()[0][:500]
            except Exception:
                pass
            logger.error(
                f"Upstream Torznab Fehler für query='{query}': {message}"
            )
            body = resp.content if resp.content.strip().startswith(b"<?xml") else (
                _torznab_error_xml(message)
            )
            raise UpstreamTorznabError(
                message,
                body=body,
                content_type=resp.headers.get(
                    "Content-Type", "application/xml; charset=UTF-8"
                ),
            )
        return resp.content
    except UpstreamTorznabError:
        raise
    except requests.RequestException as e:
        logger.error(f"Upstream Torznab Fehler für query='{query}': {e}")
        message = f"Jackett nicht erreichbar: {type(e).__name__}"
        raise UpstreamTorznabError(
            message,
            body=_torznab_error_xml(message),
        ) from e


def fetch_jackett_json_as_torznab(query: str) -> Optional[bytes]:
    """Holt Jacketts vollstaendige JSON-Suche und konvertiert sie zu Torznab.

    Einige Indexer liefern ueber den Torznab-Endpunkt weniger Treffer als ueber
    Jacketts native Results-API. Dieser Fallback wird nur fuer breite
    Staffel-Suchen verwendet.
    """
    if not JACKETT_URL or not JACKETT_API_KEY:
        return None

    indexer_id = _config.get("turktorrent_jackett_indexer_id", "turktorrent")
    url = f"{JACKETT_URL.rstrip('/')}/api/v2.0/indexers/{indexer_id}/results"
    try:
        resp = requests.get(
            url,
            params={"apikey": JACKETT_API_KEY, "Query": query, "Type": "search"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            indexer_errors = [
                str(indexer.get("Error") or "").splitlines()[0]
                for indexer in data.get("Indexers", [])
                if indexer.get("Error")
            ]
            if indexer_errors:
                raise RuntimeError(indexer_errors[0][:500])
        results = data.get("Results", []) if isinstance(data, dict) else data

        nsmap = {"torznab": TORZNAB_NS}
        rss = etree.Element("rss", version="2.0", nsmap=nsmap)
        channel = etree.SubElement(rss, "channel")
        etree.SubElement(channel, "title").text = "TurkARRBridge Jackett JSON"

        for result in results:
            title = str(result.get("Title") or "").strip()
            link = str(result.get("Link") or result.get("MagnetUri") or "").strip()
            guid = str(result.get("Guid") or link or "").strip()
            if not title or not guid:
                continue

            item = etree.SubElement(channel, "item")
            etree.SubElement(item, "title").text = title
            etree.SubElement(item, "guid", isPermaLink="false").text = guid
            etree.SubElement(item, "link").text = link
            details = str(result.get("Details") or "")
            if details:
                etree.SubElement(item, "comments").text = details

            published = result.get("PublishDate")
            if published:
                try:
                    pub_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                    etree.SubElement(item, "pubDate").text = format_datetime(pub_dt)
                except (TypeError, ValueError):
                    pass

            size = int(result.get("Size") or 0)
            etree.SubElement(item, "size").text = str(size)
            if link:
                etree.SubElement(
                    item,
                    "enclosure",
                    url=link,
                    length=str(size),
                    type="application/x-bittorrent",
                )

            seeders = int(result.get("Seeders") or 0)
            leechers = int(result.get("Peers") or 0)
            attrs = {
                "category": "5000",
                "size": str(size),
                "seeders": str(seeders),
                # Torznab definiert peers als Gesamtzahl. Jacketts JSON-Feld
                # Peers enthaelt bei diesem Indexer nur die Leecher.
                "peers": str(seeders + leechers),
                "grabs": str(int(result.get("Grabs") or 0)),
                "downloadvolumefactor": str(result.get("DownloadVolumeFactor", 1.0)),
                "uploadvolumefactor": str(result.get("UploadVolumeFactor", 1.0)),
            }
            for name, value in attrs.items():
                etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name=name, value=value)

        logger.info(f"[JACKETT-JSON] {len(results)} Treffer fuer '{query}' geladen")
        return etree.tostring(rss, xml_declaration=True, encoding="UTF-8")
    except Exception as e:
        logger.error(f"Jackett JSON-Fallback fuer query='{query}' fehlgeschlagen: {e}")
        return None


def merge_torznab_results(xml_results: list[bytes]) -> bytes:
    """
    Merged mehrere Torznab XML-Responses zu einer einzigen.
    Dedupliziert anhand der GUID.
    """
    if not xml_results:
        # Leeres Torznab-Response
        return (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
            b'xmlns:torznab="http://torznab.com/schemas/2015/feed">'
            b'<channel><title>TurkARRBridge</title></channel></rss>'
        )

    if len(xml_results) == 1:
        return xml_results[0]

    # Parse die erste Response als Basis
    try:
        base_tree = etree.fromstring(xml_results[0])
    except etree.XMLSyntaxError as e:
        logger.error(f"XML Parse-Fehler (Basis): {e}")
        return xml_results[0]

    base_channel = base_tree.find(".//channel")
    if base_channel is None:
        return xml_results[0]

    seen_guids = set()

    # Sammle GUIDs aus der Basis
    for item in base_channel.findall("item"):
        guid = item.find("guid")
        if guid is not None and guid.text:
            seen_guids.add(guid.text)

    # Merge weitere Ergebnisse
    added = 0
    for xml_data in xml_results[1:]:
        try:
            tree = etree.fromstring(xml_data)
            channel = tree.find(".//channel")
            if channel is None:
                continue

            for item in channel.findall("item"):
                guid = item.find("guid")
                guid_text = guid.text if guid is not None else None
                if guid_text and guid_text in seen_guids:
                    continue
                if guid_text:
                    seen_guids.add(guid_text)
                base_channel.append(item)
                added += 1
        except etree.XMLSyntaxError as e:
            logger.error(f"XML Parse-Fehler (Merge): {e}")
            continue

    logger.info(f"Merge: {added} neue Ergebnisse hinzugefügt (gesamt: {len(seen_guids)})")

    return etree.tostring(base_tree, xml_declaration=True, encoding="UTF-8")


# ============================================================
# Ergebnis-Cache
# ============================================================

class ResultCache:
    """Einfacher In-Memory Cache für Suchergebnisse."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._cache: dict = {}

    def _key(self, params: dict) -> str:
        sorted_params = sorted(params.items())
        return hashlib.md5(str(sorted_params).encode()).hexdigest()

    def get(self, params: dict) -> Optional[bytes]:
        key = self._key(params)
        if key in self._cache:
            entry = self._cache[key]
            if (datetime.now() - entry["time"]).total_seconds() < self.ttl:
                logger.debug(f"Ergebnis-Cache-Hit für {key}")
                return entry["data"]
            else:
                del self._cache[key]
        return None

    def set(self, params: dict, data: bytes):
        key = self._key(params)
        self._cache[key] = {"data": data, "time": datetime.now()}

        # Cleanup alte Einträge
        now = datetime.now()
        expired = [k for k, v in self._cache.items()
                   if (now - v["time"]).total_seconds() > self.ttl]
        for k in expired:
            del self._cache[k]


# ============================================================
# Flask App
# ============================================================

app = Flask(__name__)
title_cache = TitleCache(ttl_seconds=CACHE_TTL_SECONDS)
result_cache = ResultCache(ttl_seconds=CACHE_TTL_SECONDS)

# Cookie-Refresh Thread direkt starten (auch unter gunicorn)
_start_cookie_refresh_thread()

# Sonarr/Radarr-Backoff nach behobenen Trackerfehlern automatisch entfernen.
_start_arr_heal_thread()


@app.before_request
def ensure_title_cache():
    """Stellt sicher, dass der Titel-Cache aktuell ist."""
    # GUI-Routen brauchen keinen Cache-Refresh bei jedem Request
    if request.path.startswith("/gui"):
        return
    refresh_title_cache(title_cache)


@app.route("/", methods=["GET"])
def root_redirect():
    """Root → GUI wenn Browser, sonst Torznab."""
    ua = (request.headers.get("User-Agent") or "").lower()
    if any(x in ua for x in ("mozilla", "chrome", "safari", "edge", "firefox", "opera")):
        from flask import redirect
        return redirect("/gui")
    # Sonarr/Radarr und andere Clients → Torznab
    return torznab_proxy()


@app.route("/api/v2.0/indexers/turktorrent/results/torznab/", methods=["GET"])
@app.route("/api/v2.0/indexers/turktorrent/results/torznab", methods=["GET"])
@app.route("/torznab/api", methods=["GET"])
@app.route("/torznab", methods=["GET"])
@app.route("/api", methods=["GET"])
def torznab_proxy():
    """
    Haupt-Endpoint: Torznab-kompatible API.
    Fängt Suchanfragen ab, erweitert sie um türkische Titel-Varianten,
    führt mehrere Suchen durch und merged die Ergebnisse.
    """
    params = dict(request.args)
    t = params.get("t", "")
    query = params.get("q", "")

    logger.info(f"Eingehende Anfrage: t={t}, q='{query}', params={list(params.keys())}")

    # Sonarr sendet bei interaktiven Suchen normalerweise nur die TVDB-ID.
    # Der TürkTorrent-Indexer kann diese ID nicht selbst in einen Titel auflösen,
    # deshalb muss die Bridge sie vor der Upstream-Suche übersetzen.
    if t == "tvsearch" and not query and params.get("tvdbid"):
        query = title_cache.title_for_tvdb_id(params.get("tvdbid"))
        if query:
            params["q"] = query
            # Die ID nicht an einen Indexer weitergeben, der keine ID-Suche kann.
            params.pop("tvdbid", None)
            logger.info(
                f"TVDB-ID {request.args.get('tvdbid')} zu '{query}' aufgelöst"
            )

    # Nicht titelbasierte Requests direkt weiterleiten.
    if t in ("caps", "register", "tvsearch", "movie") and not query:
        params["apikey"] = JACKETT_API_KEY
        try:
            resp = requests.get(UPSTREAM_TORZNAB_URL, params=params, timeout=60)
            return Response(resp.content, content_type=resp.headers.get("Content-Type", "application/xml"))
        except Exception as e:
            logger.error(f"Upstream Fehler: {e}")
            return Response(str(e), status=502)

    # Caps direkt weiterleiten
    if t == "caps":
        params["apikey"] = JACKETT_API_KEY
        try:
            resp = requests.get(UPSTREAM_TORZNAB_URL, params=params, timeout=60)
            return Response(resp.content, content_type=resp.headers.get("Content-Type", "application/xml"))
        except Exception as e:
            return Response(str(e), status=502)

    if not query:
        # Keine Suchanfrage → direkt weiterleiten (z.B. RSS)
        params["apikey"] = JACKETT_API_KEY
        try:
            resp = requests.get(UPSTREAM_TORZNAB_URL, params=params, timeout=60)
            return Response(resp.content, content_type=resp.headers.get("Content-Type", "application/xml"))
        except Exception as e:
            return Response(str(e), status=502)

    # Ergebnis-Cache prüfen
    cached = result_cache.get(params)
    if cached:
        logger.info(f"Ergebnis aus Cache für q='{query}'")
        return Response(cached, content_type="application/xml; charset=UTF-8")

    # Suchtitel erweitern
    search_titles = title_cache.get_search_titles(query)

    # Wenn Query eine Jahreszahl enthält (z.B. "Government Woman 2 2013"),
    # versuche auch ohne Jahreszahl im Cache nachzuschlagen
    query_without_year = re.sub(r'\s+\d{4}$', '', query).strip()
    if query_without_year != query:
        search_titles_base = title_cache.get_search_titles(query_without_year)
        # Jahreszahl am Ende jedes gefundenen Titels wieder anhängen damit
        # der Indexer besser matcht – aber nur wenn sie nicht schon drin ist
        year_match = re.search(r'\d{4}$', query)
        year_str = (" " + year_match.group()) if year_match else ""
        for t in search_titles_base:
            t_with_year = t + year_str if year_str and not t.endswith(year_match.group()) else t
            if t_with_year not in search_titles:
                search_titles.append(t_with_year)
            # Auch ohne Jahr als Suchvariante behalten
            if t not in search_titles:
                search_titles.append(t)

    # Entferne Duplikate (normalisiert), aber behalte sowohl türkische als auch
    # ASCII-Varianten, da Indexer unterschiedlich benennen (z.B. "Çukur" vs "Cukur")
    seen_normalized = set()
    unique_titles = []
    # Erster Pass: Titel die sich nur in türkischen Zeichen unterscheiden
    # sollen BEIDE behalten werden (ASCII + türkisch)
    ascii_variants = {}  # norm_key -> [title1, title2, ...]
    for title in search_titles:
        norm = normalize_for_search(title)
        if not norm:
            continue
        if norm not in ascii_variants:
            ascii_variants[norm] = []
        # Nur hinzufügen wenn der exakte Text noch nicht da ist
        if title not in ascii_variants[norm]:
            ascii_variants[norm].append(title)

    for norm, titles in ascii_variants.items():
        seen_normalized.add(norm)
        # Immer alle tatsächlich verschiedenen Schreibweisen behalten
        for variant in titles:
            if variant not in unique_titles:
                unique_titles.append(variant)

    # Suchparameter ohne q und apikey
    search_params = {k: v for k, v in params.items() if k not in ("q", "apikey")}

    logger.info(f"Suche mit {len(unique_titles)} Varianten für '{query}': {unique_titles}")

    # Parallele Suchen durchführen
    xml_results = []
    upstream_errors = []
    for search_title in unique_titles:
        try:
            result = fetch_torznab(search_title, search_params)
            if result:
                xml_results.append(result)
        except UpstreamTorznabError as e:
            upstream_errors.append(e)

    # Ein kaputter Indexer ist keine erfolgreiche Suche mit null Treffern.
    # Sonarr/Radarr sollen den echten Fehler sehen und spaeter erneut versuchen.
    if not xml_results and upstream_errors:
        upstream_error = upstream_errors[0]
        return Response(
            upstream_error.body,
            status=502,
            content_type=upstream_error.content_type,
        )

    # Ergebnisse mergen
    merged = merge_torznab_results(xml_results)

    # Zähle Ergebnisse
    item_count = 0
    try:
        tree = etree.fromstring(merged)
        item_count = len(tree.findall(".//item"))
    except Exception:
        pass

    # ── Boxset-Fallback ──────────────────────────────────────────────
    # Jede tvsearch mit season-Parameter vollstaendig anreichern:
    # 1. Torznab kann trotz vorhandener Treffer weitere Episoden unterschlagen.
    # 2. Native Jackett-Suche + breite Suche liefern die Gesamtmenge.
    # 3. Passende Episoden/Season-Packs werden dedupliziert ergaenzt.
    # 4. Wenn GAR KEINE nutzbaren Releases, aber ein Multi-Season BoxSet
    #    existiert, kann dieses optional direkt an qBittorrent gesendet werden.
    #    (Sonarr meldet "Multi-season releases are not supported")
    season_param = params.get("season", "") or params.get("se", "")
    ep_param = params.get("ep", "")
    is_tvsearch = t in ("tvsearch", "search")

    if is_tvsearch and season_param:
        logger.info(
            f"[ENRICH] {item_count} Torznab-Ergebnis(se) fuer '{query}' "
            f"S{season_param} – ergaenze vollstaendige Jackett-Suche"
        )

        # Breite Suche ohne season/ep/cat-Filter (Kategorien können BoxSets verstecken)
        boxset_params = {k: v for k, v in search_params.items()
                         if k not in ("season", "se", "ep", "t", "cat", "category")}
        boxset_params["t"] = "search"

        boxset_results = []
        for search_title in unique_titles:
            try:
                result = fetch_torznab(search_title, boxset_params)
                if result:
                    boxset_results.append(result)
            except UpstreamTorznabError as e:
                logger.warning(
                    f"[ENRICH] Breite Suche fuer '{search_title}' "
                    f"fehlgeschlagen: {e}"
                )
            # Native Jackett-Suche ergaenzt Treffer, die beim Torznab-Endpoint
            # einzelner Indexer fehlen. Das Merge dedupliziert ueber die GUID.
            json_result = fetch_jackett_json_as_torznab(search_title)
            if json_result:
                boxset_results.append(json_result)

        if boxset_results:
            boxset_merged = merge_torznab_results(boxset_results)
            try:
                boxset_tree = etree.fromstring(boxset_merged)
                boxset_items = boxset_tree.findall(".//item")
                all_boxset_titles = [item.findtext("title", "") for item in boxset_items]
                logger.info(f"[BOXSET] Breite Suche: {len(boxset_items)} Ergebnisse gefunden: {all_boxset_titles[:8]}")

                season_num = season_param.zfill(2)

                # Patterns: Multi-Season BoxSets (Sonarr lehnt diese ab)
                multi_season_patterns = [
                    re.compile(r'box\s*set', re.IGNORECASE),
                    re.compile(r't[üu]m\s*sezon', re.IGNORECASE),
                    re.compile(r't[üu]m\s*b[öo]l[üu]m', re.IGNORECASE),      # Tüm Bölümler
                    re.compile(r'b[üu]t[üu]n\s*b[öo]l[üu]m', re.IGNORECASE),  # Bütün Bölümler
                    re.compile(r'komple', re.IGNORECASE),                       # Komple (= Komplett)
                    # Ueblich sind sowohl S01-S03 als auch S01-03.
                    re.compile(r'S\d{1,2}\s*-\s*S?\d{1,2}', re.IGNORECASE),
                    re.compile(r'complete\s*series', re.IGNORECASE),
                    re.compile(r'all\s*seasons', re.IGNORECASE),
                ]

                # Patterns: Einzelne Season-Packs (Sonarr versteht diese)
                single_season_patterns = [
                    re.compile(
                        rf'S{season_num}\b(?!\s*E\d)(?!\s*-\s*S?\d{{1,2}})(?!.*S\d{{2}})',
                        re.IGNORECASE
                    ),
                    re.compile(rf'Sezon\s*0?{int(season_param)}\b', re.IGNORECASE),
                    re.compile(rf'(?:^|\s){int(season_param)}\.\s*Sezon', re.IGNORECASE),
                ]

                existing_guids = set()
                try:
                    main_tree = etree.fromstring(merged)
                    for item in main_tree.findall(".//item"):
                        guid_el = item.find("guid")
                        if guid_el is not None and guid_el.text:
                            existing_guids.add(guid_el.text)
                except Exception:
                    pass

                season_packs = []   # Sonarr-kompatibel (einzelne Staffel)
                season_episodes = []  # Einzelne Folgen der angefragten Staffel
                multi_boxsets = []  # Sonarr-inkompatibel (Multi-Season)
                rejected_unrelated = []

                for item in boxset_items:
                    title_el = item.findtext("title", "")
                    guid_el = item.find("guid")
                    guid_text = guid_el.text if guid_el is not None else ""
                    if guid_text in existing_guids:
                        continue

                    # Breite Indexer-Suchen koennen beliebige populaere Releases
                    # liefern. Fremde Titel duerfen niemals automatisch in qBit.
                    if not release_matches_search(title_el, unique_titles):
                        rejected_unrelated.append(title_el)
                        continue

                    # Zuerst prüfen: Multi-Season BoxSet?
                    is_multi = any(p.search(title_el) for p in multi_season_patterns)

                    if is_multi and multi_release_covers_season(title_el, int(season_param)):
                        size_el = item.findtext("size", "0")
                        seeders = "0"
                        for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
                            if attr.get("name") == "seeders":
                                seeders = attr.get("value", "0")
                        link = item.findtext("link", "")
                        multi_boxsets.append({
                            "item": item, "title": title_el,
                            "size": int(size_el) if size_el.isdigit() else 0,
                            "seeders": int(seeders), "link": link,
                        })
                        existing_guids.add(guid_text)
                        continue

                    # Einzelfolgen aus der breiten/JSON-Suche ebenfalls an
                    # Sonarr geben. Bei Episodensuchen nur die angefragte Folge,
                    # bei Staffelsuchen alle Folgen dieser Staffel.
                    release_ep = release_episode_for_season(title_el, int(season_param))
                    if release_ep is not None:
                        if not ep_param or release_ep == int(ep_param):
                            season_episodes.append(item)
                            existing_guids.add(guid_text)
                        continue

                    # Dann prüfen: Einzelne Season für die gesuchte Staffel?
                    is_season = any(p.search(title_el) for p in single_season_patterns)
                    if is_season:
                        season_packs.append(item)
                        existing_guids.add(guid_text)

                logger.info(
                    f"[BOXSET] Gefunden: {len(season_episodes)} Episode(n), "
                    f"{len(season_packs)} Season-Pack(s), "
                    f"{len(multi_boxsets)} Multi-Season BoxSet(s); "
                    f"{len(rejected_unrelated)} fremde Treffer verworfen"
                )
                if rejected_unrelated:
                    logger.warning(
                        f"[BOXSET] Fremde Treffer fuer '{query}' nicht verwendet: "
                        f"{rejected_unrelated[:8]}"
                    )

                # Einzelne Episoden und Season-Packs an Sonarr weiterreichen.
                sonarr_releases = season_episodes + season_packs
                if sonarr_releases:
                    try:
                        main_tree = etree.fromstring(merged)
                        channel = main_tree.find(".//channel")
                        if channel is not None:
                            for item in sonarr_releases:
                                channel.append(item)
                            merged = etree.tostring(main_tree, xml_declaration=True,
                                                    encoding="UTF-8", pretty_print=True)
                            item_count += len(sonarr_releases)
                            logger.info(
                                f"[BOXSET] {len(season_episodes)} Episode(n) + "
                                f"{len(season_packs)} Season-Pack(s) → Sonarr "
                                f"für '{query}' S{season_param}"
                            )
                    except Exception as e:
                        logger.error(f"[BOXSET] Merge-Fehler: {e}")

                # Wenn KEINE Season-Packs gefunden, aber BoxSets vorhanden
                # → Bestes BoxSet direkt an qBittorrent senden (Sonarr kann's nicht)
                if (item_count == 0 and not sonarr_releases and multi_boxsets
                        and BOXSET_AUTO_DOWNLOAD and BRIDGE_DIRECT_DOWNLOADS_ENABLED):
                    # Qualitäts-Score: höher = besser
                    def _quality_score(info):
                        t = info["title"]
                        if re.search(r'2160p|4k|uhd', t, re.I):
                            q = 4
                        elif re.search(r'1080p', t, re.I):
                            q = 3
                        elif re.search(r'720p', t, re.I):
                            q = 2
                        else:
                            q = 1
                        # Codec-Bonus: H.265/HEVC > H.264
                        if re.search(r'H\.?265|HEVC|x265', t, re.I):
                            q += 0.5
                        return q

                    if BOXSET_PREFER_SEEDERS:
                        # Seeder-Modus: Meiste Seeders → Höchste Qualität → Kleinste Größe
                        best = sorted(multi_boxsets,
                                      key=lambda x: (-x["seeders"], -_quality_score(x), x["size"]))[0]
                    else:
                        # Qualitäts-Modus (default): Höchste Qualität → Meiste Seeders → Kleinste Größe
                        best = sorted(multi_boxsets,
                                      key=lambda x: (-_quality_score(x), -x["seeders"], x["size"]))[0]

                    mode = 'Seeders' if BOXSET_PREFER_SEEDERS else 'Qualit\u00e4t'
                    logger.info(f"[BOXSET] Kein Season-Pack verf\u00fcgbar \u2192 sende BoxSet direkt an qBit: {best['title']} (Q={_quality_score(best)}, Seeds={best['seeders']}, Modus={mode})")

                    # Async an qBit senden, damit die Antwort nicht blockiert
                    def _async_boxset_download(boxset_info, series_query, season):
                        link = boxset_info["link"]
                        title = boxset_info["title"]
                        size_gb = boxset_info["size"] / (1024**3) if boxset_info["size"] else 0

                        if not link:
                            logger.error("[BOXSET] Kein Download-Link verfügbar")
                            return

                        success = qbit_add_torrent(link, category="tv-tr-boxset")
                        if success:
                            logger.info(f"[BOXSET] ✅ BoxSet '{title}' erfolgreich an qBit gesendet")
                            send_telegram(
                                f"📦 <b>BoxSet Auto-Download</b>\n\n"
                                f"🔍 Gesucht: <code>{series_query} S{season}</code>\n"
                                f"📀 <b>{title}</b>\n"
                                f"💾 {size_gb:.1f} GB\n"
                                f"🌱 {boxset_info['seeders']} Seeders\n\n"
                                f"ℹ️ Sonarr konnte kein Season-Pack finden.\n"
                                f"Die Bridge hat das BoxSet direkt an qBittorrent gesendet."
                            )
                        else:
                            logger.error(f"[BOXSET] ❌ BoxSet-Download fehlgeschlagen: {title}")
                            send_telegram(
                                f"❌ <b>BoxSet Download fehlgeschlagen</b>\n\n"
                                f"🔍 Gesucht: <code>{series_query} S{season}</code>\n"
                                f"📀 {title}\n\n"
                                f"Bitte manuell herunterladen."
                            )

                    threading.Thread(
                        target=_async_boxset_download,
                        args=(best, query, season_param),
                        daemon=True
                    ).start()

                    # Trotzdem das BoxSet auch in die Torznab-Antwort aufnehmen
                    # (Sonarr wird es ablehnen, aber es ist sichtbar)
                    try:
                        main_tree = etree.fromstring(merged)
                        channel = main_tree.find(".//channel")
                        if channel is not None:
                            channel.append(best["item"])
                            merged = etree.tostring(main_tree, xml_declaration=True,
                                                    encoding="UTF-8", pretty_print=True)
                            item_count += 1
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"[BOXSET] Parse-Fehler: {e}")

    # ── Title-Rewrite: türkische Titel → internationaler Suchbegriff ──────────
    # Radarr/Sonarr matchen Releases am Titel. Wenn der Torrent-Titel türkisch ist
    # (z.B. "Hükümet Kadın 2") und Radarr nach "Government Woman 2" sucht, lehnt
    # Radarr das Release ab. Wir ersetzen den Titel im XML durch den Radarr-Titel.
    try:
        rewrite_tree = etree.fromstring(merged)
        query_norm = normalize_for_search(query)

        # Den "kanonischen" Radarr/Sonarr-Titel aus dem Cache ermitteln:
        # Das ist der Titel unter dem das Objekt in Radarr/Sonarr gelistet ist.
        # Er ist meist der einzige nicht-türkische Titel im Mapping.
        canonical_title = query  # Fallback: Suchbegriff selbst
        cache_key = title_cache._cache_key(query)
        # Auch ohne Jahreszahl suchen (z.B. "Sut Kardesler 1976" → "sut kardesler")
        cache_key_no_year = re.sub(r'\s*\d{4}$', '', cache_key).strip()
        _ck = cache_key if cache_key in title_cache._cache else (
              cache_key_no_year if cache_key_no_year in title_cache._cache else None)
        if _ck is not None:
            all_cached = list(title_cache._cache[_ck]["titles"])
            TR_CHARS = set("İıŞşĞğÜüÖöÇç")
            # Alle Titel ohne Jahresangabe als Kandidaten
            candidates = [t for t in all_cached if not re.match(r'^\d{4}$', t.strip())]

            # Strategie 1: Suche den Titel, dessen ASCII-Normalisierung am besten
            # zum Query passt. Bei mehreren Treffern wird der Titel MIT türkischen
            # Zeichen bevorzugt (= echter Radarr-Haupttitel wie 'Süt Kardeşler').
            query_ascii = normalize_for_search(strip_turkish_chars(query))
            # Auch ohne Jahreszahl vergleichen (Cache hat keine Jahre in Titeln)
            query_ascii_no_year = re.sub(r'\s*\d{4}$', '', query_ascii).strip()
            ascii_matches = []
            for t in candidates:
                t_ascii = normalize_for_search(strip_turkish_chars(t))
                if t_ascii == query_ascii or t_ascii == query_ascii_no_year:
                    ascii_matches.append(t)
            if not ascii_matches:
                for t in candidates:
                    t_ascii = normalize_for_search(strip_turkish_chars(t))
                    if (query_ascii_no_year and t_ascii.startswith(query_ascii_no_year)):
                        ascii_matches.append(t)

            best_match = None
            if ascii_matches:
                # Bevorzuge Titel mit türkischen Sonderzeichen (= echter Radarr-Titel)
                turkish_pref = [t for t in ascii_matches if any(c in t for c in TR_CHARS)]
                best_match = turkish_pref[0] if turkish_pref else ascii_matches[0]

            if best_match:
                canonical_title = best_match
                logger.debug(f"[REWRITE] Best-Match für '{query}': '{canonical_title}'")
            else:
                # Strategie 2: kürzester nicht-türkischer Titel (internationaler Titel)
                non_turkish = [t for t in candidates if not any(c in t for c in TR_CHARS)]
                if non_turkish:
                    canonical_title = min(non_turkish, key=len)
                    logger.debug(f"[REWRITE] Kanonischer Titel für '{query}': '{canonical_title}'")
                # Strategie 3: Kein passender Titel → ASCII-Fallback
                elif any(c in query for c in TR_CHARS):
                    canonical_title = strip_turkish_chars(query)
                    logger.debug(f"[REWRITE] ASCII-Fallback für '{query}': '{canonical_title}'")

        canonical_norm = normalize_for_search(canonical_title)

        # Qualitäts-Tokens die wir aus dem Original-Titel extrahieren und anhängen
        QUALITY_RE = re.compile(
            r'(4K|2160p|1080p|720p|480p|BDRip|BluRay|Blu-Ray|WEBRip|WEB-DL|HDTV|'
            r'DVDRip|DVD|x264|x265|H\.264|H\.265|HEVC|AVC|AAC|AC3|DDP5|DTS|'
            r'PROPER|REPACK|REMUX|HDR|SDR|Dual|TR|ENG|TurkHD|Turkish|[Ss]\d+[Ee]?\d*)',
            re.IGNORECASE
        )

        rewritten = 0
        for item in rewrite_tree.findall(".//item"):
            t_el = item.find("title")
            if t_el is None or not t_el.text:
                continue
            orig_title = t_el.text
            orig_norm = normalize_for_search(orig_title)
            # ASCII-normalisierter Vergleich (türkische Sonderzeichen ignorieren)
            orig_ascii = normalize_for_search(strip_turkish_chars(orig_title))
            canonical_ascii = normalize_for_search(strip_turkish_chars(canonical_title))

            # Kein Rewrite wenn der Originaltitel bereits EXAKT den kanonischen Titel enthält
            # (beide normalisiert vergleichen – aber nur überspringen wenn canonical nicht-leer)
            if canonical_norm and len(canonical_norm) >= 3 and orig_norm == canonical_norm:
                continue
            # Überspringen wenn orig bereits mit canonical beginnt UND canonical == query
            # (= kein sinnvoller Rewrite möglich, z.B. Film hat keinen anderen Titel)
            if (canonical_norm and orig_norm.startswith(canonical_norm + " ")
                    and normalize_for_search(canonical_title) == normalize_for_search(query)):
                continue
            # Wenn canonical türkische Zeichen hat, aber orig kein einziges → Rewrite!
            # (= ASCII-Torrent soll auf UTF-8-Titel umgeschrieben werden)
            # ASCII-Variante: "Sut Kardesler 1080p" soll rewritten werden zu "Süt Kardeşler 1080p"
            # → NICHT überspringen, auch wenn ASCII-normalisiert gleich — Rewrite ist erwünscht!
            # Nur überspringen wenn ASCII orig == ASCII canonical exakt (kein Qualitäts-Suffix)
            # und canonical bereits kein türkisches Zeichen hat
            if (canonical_ascii and len(canonical_ascii) >= 3
                    and orig_ascii == canonical_ascii
                    and not any(c in canonical_title for c in TR_CHARS)):
                continue

            # ── Neuen Titel zusammenbauen ──
            # Strategie: Nur den Filmname-Teil des Torrent-Titels ersetzen,
            # den Rest (Jahr, Qualität, Release-Group) beibehalten.
            # Dazu finden wir die Position ab der Meta-Info beginnt.

            # Jahr extrahieren (z.B. "1976", "(1976)", "[1976]")
            year_match = re.search(r'[\(\[]?(\d{4})[\)\]]?', orig_title)
            year_str = ""
            meta_start = len(orig_title)
            if year_match:
                year_str = year_match.group(0)  # z.B. "(1976)" oder "1976"
                meta_start = year_match.start()

            # Staffelmarker muessen Teil des Suffix bleiben. Tuerkische Tracker
            # verwenden neben S01 auch "Sezon 1" und "1. Sezon".
            season_marker = re.search(
                r'\bS\d{1,2}(?:\s*E\d{1,3})?(?:\s*-\s*S?\d{1,2})?\b|'
                r'\bSezon\s*0?\d{1,2}\b|\b\d{1,2}\.\s*Sezon\b',
                orig_title,
                re.IGNORECASE,
            )
            if season_marker and season_marker.start() < meta_start:
                meta_start = season_marker.start()

            # Falls kein Jahr/Staffelmarker gefunden: Meta beginnt beim ersten Quality-Token
            if not year_match:
                first_q = QUALITY_RE.search(orig_title)
                if first_q and first_q.start() < meta_start:
                    meta_start = first_q.start()

            # Alles ab meta_start ist der "Suffix" (Jahr + Quality + Release-Group)
            suffix = orig_title[meta_start:].strip()

            # Tuerkische Staffelnotation in Sonarrs erwartetes Sxx umwandeln.
            suffix = re.sub(
                r'^Sezon\s*0?(\d{1,2})\b',
                lambda m: f"S{int(m.group(1)):02d}",
                suffix,
                flags=re.IGNORECASE,
            )
            suffix = re.sub(
                r'^(\d{1,2})\.\s*Sezon\b',
                lambda m: f"S{int(m.group(1)):02d}",
                suffix,
                flags=re.IGNORECASE,
            )

            # Falls query ein Jahr enthält, aber der Orig-Titel keins hat → Jahr aus Query nehmen
            if not year_match:
                q_year = re.search(r'\b(\d{4})\b', query)
                if q_year:
                    year_str = q_year.group(1)
                    suffix = f"{year_str} {suffix}" if suffix else year_str

            # Neuer Titel: kanonischer Titel + Suffix (enthält Jahr, Qualität, Release-Group)
            new_title = f"{canonical_title} {suffix}" if suffix else canonical_title
            t_el.text = new_title
            rewritten += 1
            logger.debug(f"[REWRITE] '{orig_title}' → '{new_title}'")

        if rewritten:
            logger.info(f"[REWRITE] {rewritten} Titel → '{canonical_title}' für besseres Radarr/Sonarr-Matching")
            merged = etree.tostring(rewrite_tree, xml_declaration=True, encoding="UTF-8")
    except Exception as e:
        logger.error(f"[REWRITE] Fehler beim Titel-Rewrite: {e}")

    # In Cache speichern
    result_cache.set(params, merged)

    logger.info(f"Antwort: {item_count} Ergebnisse für '{query}'")
    return Response(merged, content_type="application/xml; charset=UTF-8")


@app.route("/health", methods=["GET"])
def health():
    """Health-Check Endpoint."""
    stats = title_cache.stats()
    with _arr_heal_status_lock:
        arr_heal = dict(_arr_heal_status)
    return {
        "status": "ok",
        "bridge": "TurkARRBridge",
        "version": "1.0.0",
        "title_cache": stats,
        "upstream": UPSTREAM_TORZNAB_URL,
        "sonarr": SONARR_URL,
        "radarr": RADARR_URL,
        "arr_indexer_auto_heal": arr_heal,
    }


@app.route("/mappings", methods=["GET"])
def show_mappings():
    """Zeigt alle bekannten Titel-Mappings an (Debug-Endpoint)."""
    refresh_title_cache(title_cache)
    mappings = {}
    for key, entry in title_cache._cache.items():
        mappings[key] = list(entry["titles"])
    return {
        "count": len(mappings),
        "mappings": mappings
    }


@app.route("/test/<path:query>", methods=["GET"])
def test_search(query: str):
    """Test-Endpoint: Zeigt, welche Titel für eine Suchanfrage verwendet werden und was gefunden wird."""
    refresh_title_cache(title_cache)
    titles = title_cache.get_search_titles(query)

    # Auch tatsächlich bei Jackett suchen
    results_by_source = {}
    total_results = 0
    seen_normalized = set()
    unique_titles = []
    for title in titles:
        norm = normalize_for_search(title)
        if norm and norm not in seen_normalized:
            seen_normalized.add(norm)
            unique_titles.append(title)

    for search_title in unique_titles:
        try:
            resp = requests.get(
                UPSTREAM_TORZNAB_URL,
                params={"apikey": JACKETT_API_KEY, "q": search_title, "t": "search"},
                timeout=30
            )
            if resp.ok:
                tree = etree.fromstring(resp.content)
                items = tree.findall(".//item")
                result_titles = []
                for item in items:
                    t_el = item.find("title")
                    if t_el is not None and t_el.text:
                        result_titles.append({"title": t_el.text})
                results_by_source[search_title] = result_titles
                total_results += len(result_titles)
        except Exception as e:
            results_by_source[search_title] = [{"error": str(e)}]

    return {
        "query": query,
        "search_variants": unique_titles,
        "search_titles": titles,
        "count": len(titles),
        "total_results": total_results,
        "results_by_source": results_by_source,
    }


# ============================================================
# GUI – Web-Interface für Konfiguration & Tests
# ============================================================

GUI_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Türk ARR Bridge</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🇹🇷</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0f1117;--card:#1a1d27;--card2:#232738;--border:#2d3348;--accent:#6c5ce7;
--accent2:#a29bfe;--green:#00b894;--red:#ff6b6b;--orange:#fdcb6e;--text:#e2e8f0;
--text2:#94a3b8;--text3:#64748b;--radius:12px;--shadow:0 4px 24px rgba(0,0,0,.3)}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:flex-start}
.header{width:100%;background:linear-gradient(135deg,#12151f 0%,#2a1520 45%,#12151f 100%);border-bottom:2px solid #c8102e60;padding:24px 32px;display:flex;align-items:center;justify-content:center;gap:14px;position:relative;overflow:hidden;flex-shrink:0}
.header::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,transparent 5%,#c8102e 25%,#fff 50%,#c8102e 75%,transparent 95%)}
.header::before{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,#c8102e60,transparent)}
.header-center{display:flex;flex-direction:column;align-items:center;gap:4px}
.header-title{display:flex;align-items:center;gap:14px}
.header-title h1{font-size:2rem;font-weight:800;letter-spacing:-.02em;background:linear-gradient(135deg,#ffffff 0%,#f8c8d4 40%,#c8102e 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-shadow:none;filter:drop-shadow(0 2px 8px rgba(200,16,46,.35))}
.header-sub{font-size:.8rem;color:var(--text2);letter-spacing:.06em;text-transform:uppercase;opacity:.7}
.flag-anim{font-size:1.6rem;display:inline-block;animation:flagFloat 3s ease-in-out infinite}
@keyframes flagFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.audio-btn{background:rgba(200,16,46,.15);border:1px solid #c8102e40;color:#c8102e;width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:.9rem;transition:all .25s;z-index:10;flex-shrink:0}
.audio-btn:hover{background:rgba(200,16,46,.3);transform:scale(1.1)}
.audio-btn.playing{background:rgba(200,16,46,.35);box-shadow:0 0 12px #c8102e60;animation:audioPulse 1.5s ease-in-out infinite}
@keyframes audioPulse{0%,100%{box-shadow:0 0 8px #c8102e40}50%{box-shadow:0 0 18px #c8102e80}}
/* Header rechte Seite: Audio + Profil */
.header-right{position:absolute;right:24px;top:50%;transform:translateY(-50%);display:flex;align-items:center;gap:10px;z-index:10}
/* Profil-Dropdown */
.profile-wrap{position:relative}
.profile-btn{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:var(--text);width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:1.05rem;transition:all .2s}
.profile-btn:hover{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.25)}
.profile-dropdown{display:none;position:fixed;background:#1e2130;border:1px solid var(--border);border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.85),0 2px 8px rgba(0,0,0,.5);min-width:180px;overflow:hidden;z-index:99999}
.profile-dropdown.open{display:block}
.profile-dropdown-header{padding:12px 16px 10px;font-size:.82rem;font-weight:600;color:var(--text2);border-bottom:1px solid var(--border);background:#191c2a}
.profile-dropdown-divider{height:1px;background:var(--border);margin:2px 0}
.profile-dropdown-item{display:flex;align-items:center;gap:10px;padding:11px 16px;font-size:.84rem;color:var(--text2);cursor:pointer;transition:background .15s}
.profile-dropdown-item:hover{background:rgba(255,255,255,.06);color:var(--text)}
.profile-dropdown-item.danger{color:#ff6b6b}
.profile-dropdown-item.danger:hover{background:rgba(255,107,107,.1)}
/* Footer-Bar unten */
.sub-header{width:100%;background:#0d0f18;border-top:1px solid #1e2235;padding:6px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-shrink:0;margin-top:auto}
.sub-header-version{font-size:.72rem;color:var(--text3);letter-spacing:.05em;font-family:'JetBrains Mono','Fira Code',monospace}
.sub-header-status{display:flex;align-items:center;gap:5px;font-size:.72rem;color:var(--text3)}
.sub-header-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:dotPulse 2.5s ease-in-out infinite}
@keyframes dotPulse{0%,100%{opacity:1}50%{opacity:.45}}
.lang-switcher{position:absolute;left:24px;top:50%;transform:translateY(-50%);display:flex;gap:6px;z-index:10}
.lang-btn{font-size:1.4rem;cursor:pointer;border:2px solid transparent;border-radius:6px;padding:2px 4px;transition:all .2s;opacity:.55;line-height:1;background:none}
.lang-btn:hover{opacity:1;transform:scale(1.15)}
.lang-btn.active{opacity:1;border-color:rgba(255,255,255,.5);background:rgba(255,255,255,.1);transform:scale(1.1)}
/* Layout: zentrierter Bereich, Hintergrund links/rechts sichtbar */
.app-body{display:flex;gap:20px;width:100%;max-width:1200px;padding:20px 20px;align-items:flex-start;flex:1}
/* Sidebar: schwebendes Panel, endet mit den Inhalten */
.sidebar{width:230px;flex-shrink:0;background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
.sidebar-label{font-size:.68rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);padding:14px 14px 4px}
.sidebar-section{padding:6px 8px}
/* Sidebar-Group-Header (klappbar) */
.sidebar-group{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:9px;cursor:pointer;font-size:.92rem;font-weight:600;color:var(--text2);transition:all .2s;user-select:none}
.sidebar-group:hover{color:var(--text);background:var(--card2)}
.sidebar-group.active{color:var(--text);background:var(--card2)}
.sidebar-group .chevron{margin-left:auto;font-size:.65rem;transition:transform .2s;opacity:.5}
.sidebar-group.open .chevron{transform:rotate(90deg)}
.sidebar-items{display:none;padding:0 0 4px 0}
.sidebar-items.open{display:block}
/* Einzelne Items: echte anklickbare Kacheln mit eigenem Hintergrund */
.sidebar-item{display:flex;align-items:center;gap:10px;padding:10px 12px;margin:5px 0;border-radius:9px;cursor:pointer;font-size:.92rem;font-weight:500;color:var(--text2);transition:all .15s;background:var(--card2);border:1px solid var(--border);box-shadow:0 2px 8px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.04)}
.sidebar-item:hover{color:var(--text);background:#2a2e42;border-color:#3d4460;box-shadow:0 3px 12px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.04)}
.sidebar-item.active{color:#fff;background:var(--accent);font-weight:600;border-color:var(--accent);box-shadow:0 2px 10px rgba(108,92,231,.4),inset 0 1px 0 rgba(255,255,255,.1)}
/* Dashboard-Item oben */
.sidebar-dashboard{display:flex;align-items:center;gap:10px;padding:10px 12px;margin:5px 0;border-radius:9px;cursor:pointer;font-size:.92rem;font-weight:500;color:var(--text2);transition:all .15s;background:var(--card2);border:1px solid var(--border);box-shadow:0 2px 8px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.04)}
.sidebar-dashboard:hover{color:var(--text);background:#2a2e42;border-color:#3d4460;box-shadow:0 3px 12px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.04)}
.sidebar-dashboard.active{color:#fff;background:var(--accent);font-weight:600;border-color:var(--accent);box-shadow:0 2px 10px rgba(108,92,231,.4),inset 0 1px 0 rgba(255,255,255,.1)}
.sidebar-divider{height:1px;background:var(--border);margin:4px 8px}
/* Main content */
.main-content{flex:1;min-width:0}
.panel{display:none}
.panel.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;margin-bottom:14px;box-shadow:var(--shadow)}
.card h2{font-size:1rem;font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}.sidebar{width:190px}}
@media(max-width:640px){.app-body{flex-direction:column}.sidebar{width:100%}}
.field{margin-bottom:8px}
.field label{display:block;font-size:.78rem;font-weight:500;color:var(--text2);margin-bottom:3px}
.field input,.field select{width:100%;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.85rem;transition:border .2s}
.field input:focus,.field select:focus{outline:none;border-color:var(--accent2)}
.field input.ok{border-color:var(--green)}
.field input.err{border-color:var(--red)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:8px 16px;border:none;border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer;transition:all .2s}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent2)}
.btn-success{background:var(--green);color:#fff}
.btn-danger{background:var(--red);color:#fff}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text2)}
.btn-outline:hover{border-color:var(--accent2);color:var(--text)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-row{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap}
.status{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.75rem;font-weight:600}
.status.ok{background:rgba(0,184,148,.15);color:var(--green)}
.status.err{background:rgba(255,107,107,.15);color:var(--red)}
.status.warn{background:rgba(253,203,110,.15);color:var(--orange)}
.status.loading{background:rgba(108,92,231,.15);color:var(--accent2)}
.conn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
/* App-Cards wie im Screenshot: dunkle Fläche, schwarzer Schatten, kein farbiger Rahmen */
.conn-card{background:var(--card2);border:1px solid var(--border);border-radius:12px;padding:18px 16px 14px 16px;position:relative;transition:box-shadow .25s,transform .2s;box-shadow:0 4px 20px rgba(0,0,0,.55),0 1px 0 rgba(255,255,255,.03) inset;cursor:default}
.conn-card:hover{box-shadow:0 8px 32px rgba(0,0,0,.7),0 1px 0 rgba(255,255,255,.04) inset;transform:translateY(-2px)}
.conn-card.ok{border-color:var(--border)}
.conn-card.err{border-color:var(--border)}
.conn-card .app-icon{font-size:2rem;margin-bottom:8px;display:block;line-height:1}.conn-card .app-icon-img{width:42px;height:42px;margin-bottom:8px;display:block;object-fit:contain;border-radius:8px}
.conn-card .name{font-weight:700;font-size:1rem;margin-bottom:2px;color:var(--text)}
.conn-card .url{font-size:.72rem;color:var(--text3);word-break:break-all;margin-top:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.conn-card .detail{font-size:.78rem;color:var(--text2);margin-top:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-all}
.conn-card .status-row{display:flex;align-items:center;gap:5px;position:absolute;top:12px;right:12px}
.conn-card .status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.conn-card .status-dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.conn-card .status-dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
.conn-card .status-dot.loading{background:var(--orange);box-shadow:0 0 6px var(--orange)}
.conn-card .status-text{font-size:.7rem;color:var(--text2)}
.log-box{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:'JetBrains Mono','Fira Code',monospace;font-size:.78rem;line-height:1.6;max-height:600px;overflow-y:auto;color:var(--text2);white-space:pre-wrap;word-break:break-all}
.search-box{display:flex;gap:10px;margin-bottom:16px}
.search-box input{flex:1}
.result-item{padding:10px 14px;border-bottom:1px solid var(--border);font-size:.85rem}
.result-item:last-child{border-bottom:none}
.result-item .src{color:var(--accent2);font-weight:500;font-size:.75rem;margin-bottom:2px}
.mapping-list{max-height:500px;overflow-y:auto}
.mapping-item{display:flex;justify-content:space-between;padding:8px 14px;border-bottom:1px solid var(--border);font-size:.82rem}
.mapping-item:hover{background:var(--card2)}
.mapping-item .key{color:var(--accent2);font-weight:500;min-width:200px}
.mapping-item .vals{color:var(--text2);text-align:right;flex:1}
.toast{position:fixed;bottom:24px;right:24px;padding:14px 24px;border-radius:10px;font-size:.85rem;font-weight:500;z-index:9999;animation:slideIn .3s;box-shadow:var(--shadow)}
.toast.ok{background:var(--green);color:#fff}
.toast.err{background:var(--red);color:#fff}
@keyframes slideIn{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent2);border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#updateBanner{display:none;position:fixed;bottom:24px;right:24px;z-index:9998;background:linear-gradient(135deg,#1e1b4b,#312e81);border:1px solid #6366f1;border-radius:12px;padding:14px 18px;max-width:340px;box-shadow:0 8px 32px rgba(0,0,0,.6);animation:slideIn .4s}
#updateBanner .ub-title{font-size:.85rem;font-weight:600;color:#a5b4fc;margin-bottom:4px}
#updateBanner .ub-msg{font-size:.75rem;color:#c7d2fe;line-height:1.4}
#updateBanner .ub-actions{display:flex;gap:8px;margin-top:10px;align-items:center}
#updateBanner .ub-btn{padding:5px 14px;border-radius:6px;font-size:.75rem;font-weight:600;cursor:pointer;border:none}
#updateBanner .ub-btn.primary{background:#6366f1;color:#fff}
#updateBanner .ub-btn.primary:hover{background:#818cf8}
#updateBanner .ub-btn.dismiss{background:transparent;color:#94a3b8;text-decoration:underline}
#updateBanner .ub-close{position:absolute;top:6px;right:10px;background:none;border:none;color:#94a3b8;font-size:1.1rem;cursor:pointer}
.hint{font-size:.72rem;color:var(--text3);margin-top:2px;line-height:1.3}
</style>
</head>
<body>
<!-- Captcha Notification Banner -->
<div id="captchaBanner" style="display:none;position:fixed;top:0;left:0;right:0;z-index:9999;background:linear-gradient(135deg,#2a1a0a,#3a2010);border-bottom:2px solid #f59e0b;padding:12px 20px;text-align:center;animation:slideDown .4s ease-out;box-shadow:0 4px 24px rgba(245,158,11,.25)">
<style>
@keyframes slideDown{from{transform:translateY(-100%);opacity:0}to{transform:translateY(0);opacity:1}}
@keyframes captchaPulse{0%,100%{opacity:1}50%{opacity:.6}}
#captchaBanner .cb-inner{display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap}
#captchaBanner .cb-icon{font-size:1.4rem;animation:captchaPulse 1.5s ease-in-out infinite}
#captchaBanner .cb-text{color:#fbbf24;font-size:.9rem;font-weight:600}
#captchaBanner .cb-btn{background:#f59e0b;color:#1a1a1a;border:none;padding:6px 16px;border-radius:8px;font-size:.82rem;font-weight:700;cursor:pointer;text-decoration:none;transition:all .2s}
#captchaBanner .cb-btn:hover{background:#fbbf24;transform:scale(1.05)}
#captchaBanner .cb-close{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:#fbbf2480;font-size:1.2rem;cursor:pointer;padding:4px 8px}
#captchaBanner .cb-close:hover{color:#fbbf24}
</style>
<div class="cb-inner">
<span class="cb-icon">🔐</span>
<span class="cb-text">hCaptcha-Lösung benötigt – TurkTorrent Session abgelaufen</span>
<a class="cb-btn" href="/captcha" target="_blank">Captcha jetzt lösen</a>
</div>
<button class="cb-close" onclick="document.getElementById('captchaBanner').style.display='none'">&times;</button>
</div>
<script>
(function(){
  let _lastCaptchaState = false;
  function pollCaptcha(){
    fetch('/captcha-status').then(r=>r.json()).then(d=>{
      const banner = document.getElementById('captchaBanner');
      if(d.active && d.waiting){
        if(!_lastCaptchaState) banner.style.display='block';
        _lastCaptchaState = true;
      } else {
        banner.style.display='none';
        _lastCaptchaState = false;
      }
    }).catch(()=>{});
  }
  setInterval(pollCaptcha, 10000);
  setTimeout(pollCaptcha, 2000);
})();
</script>
<div class="header">
<div class="lang-switcher">
<button class="lang-btn" id="lang-de" onclick="setLang('de')" title="Deutsch">🇩🇪</button>
<button class="lang-btn" id="lang-en" onclick="setLang('en')" title="English">🇺🇸</button>
<button class="lang-btn active" id="lang-tr" onclick="setLang('tr')" title="Türkçe">🇹🇷</button>
</div>
<div class="header-center">
<div class="header-title">
<h1>Türk ARR Bridge</h1>
<span class="flag-anim">🇹🇷</span>
</div>
<div class="header-sub" data-i18n="header_sub">Torznab Proxy für türkische Serien & Filme</div>
</div>
<div class="header-right">
<div class="audio-btn" id="audioBtn" onclick="toggleMusic()" title="🎵 Türkçe Müzik">▶</div>
<div class="profile-wrap">
<div class="profile-btn" id="profileBtn" onclick="toggleProfileDropdown(event)" title="Profil">👤</div>
</div>
</div>
<audio id="headerAudio" src="/gui/music.mp3" preload="none" loop></audio>
</div>
<!-- Dropdown als body-Portal: nie vom header abgeschnitten -->
<div class="profile-dropdown" id="profileDropdown">
<div class="profile-dropdown-header">👤 <span id="ddUsername">...</span></div>
<div class="profile-dropdown-divider"></div>
<div class="profile-dropdown-item danger" onclick="window.location.href='/gui/logout'">🚪 <span data-i18n="btn_logout">Abmelden</span></div>
</div>
<div class="app-body">
<!-- SIDEBAR -->
<nav class="sidebar" id="sidebar">

<!-- Dashboard -->
<div class="sidebar-section">
<div class="sidebar-dashboard active" data-panel="dashboard" onclick="navTo('dashboard',this)">🏠 <span data-i18n="nav_dashboard">Dashboard</span></div>
</div>

<div class="sidebar-divider"></div>

<!-- Verbindungen mit Unterpunkt Tools + Indexer -->
<div class="sidebar-section">
<div class="sidebar-label" data-i18n="nav_connections">Verbindungen</div>
<div class="sidebar-item" data-panel="connections" onclick="navTo('connections',this)">🛠️ <span data-i18n="nav_tools">Tools</span></div>
<div class="sidebar-item" data-panel="indexer" onclick="navTo('indexer',this)">🔎 <span data-i18n="nav_indexer">Indexer</span></div>
</div>

<div class="sidebar-divider"></div>

<!-- Einstellungen -->
<div class="sidebar-section">
<div class="sidebar-label" data-i18n="nav_settings">Einstellungen</div>
<div class="sidebar-item" data-panel="settings-tuning" onclick="navTo('settings-tuning',this)">🏛️ <span data-i18n="nav_tuning">Tuning</span></div>
<div class="sidebar-item" data-panel="settings-system" onclick="navTo('settings-system',this)">⚙️ <span data-i18n="nav_system">System</span></div>
</div>

<div class="sidebar-divider"></div>

<!-- Notifikationen -->
<div class="sidebar-section">
<div class="sidebar-label" data-i18n="nav_notifications">Notifikationen</div>
<div class="sidebar-item" data-panel="notif-telegram" onclick="navTo('notif-telegram',this)">📨 <span data-i18n="nav_telegram">Telegram</span></div>
</div>

<div class="sidebar-divider"></div>

<!-- Info -->
<div class="sidebar-section">
<div class="sidebar-label" data-i18n="nav_info">Info</div>
<div class="sidebar-item" data-panel="search" onclick="navTo('search',this)">🔍 <span data-i18n="nav_search">Suche testen</span></div>
<div class="sidebar-item" data-panel="mappings" onclick="navTo('mappings',this)">📖 <span data-i18n="nav_mappings">Titel-Mappings</span></div>
<div class="sidebar-item" data-panel="learned" onclick="navTo('learned',this)">🧠 <span data-i18n="nav_learned">Gelernt</span></div>
<div class="sidebar-item" data-panel="logs" onclick="navTo('logs',this)">📋 <span data-i18n="nav_logs">Logs</span></div>
</div>

<div style="padding-bottom:8px"></div>
</nav>
<!-- MAIN -->
<div class="main-content">

<!-- DASHBOARD -->
<div class="panel active" id="panel-dashboard">

<div class="card"><h2>🔗 <span data-i18n="dash_connections">Verbindungen</span></h2>
<div class="conn-grid" id="connGrid"></div>
<div class="btn-row" style="justify-content:flex-end"><button class="btn btn-primary" onclick="testAllConnections()">🔄 <span data-i18n="btn_test_all">Alle testen</span></button></div>
</div>

<div class="card"><h2>🔎 Indexer</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
<div class="conn-card" id="indexerStatusCard" style="cursor:pointer" onclick="navTo('indexer', document.querySelector('[data-panel=indexer]'))">
  <img class="app-icon-img" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgcng9IjE4IiBmaWxsPSIjYzAzOTJiIi8+PHRleHQgeD0iNTAiIHk9IjY1IiBmb250LXNpemU9IjM4IiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiIgZm9udC13ZWlnaHQ9ImJvbGQiIGZpbGw9IiNmZmYiPlRSPC90ZXh0Pjwvc3ZnPg==" alt="TurkTorrent">
  <div class="name">TurkTorrent</div>
  <div class="status-row">
    <div class="status-dot" id="indexerStatusDot"></div>
    <span class="status-text" id="indexerStatusText">Prüfe...</span>
  </div>
  <div class="detail" id="indexerStatusDetail">via Jackett</div>
</div>
</div>
<div class="btn-row" style="justify-content:flex-end">
  <button class="btn btn-outline" onclick="refreshCookieNow()">🍪 Cookie jetzt aktualisieren</button>
</div>
</div>

</div>

<!-- VERBINDUNGEN (2x2 Grid) -->
<div class="panel" id="panel-connections">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">

<div class="card"><h2><span class="tool-icon" id="tool-icon-sonarr">📺</span> Sonarr</h2>
<div class="field"><label data-i18n="lbl_url">URL</label><input id="cfg_sonarr_url" placeholder="http://your-nas:8989"></div>
<div class="field"><label data-i18n="lbl_api_key">API Key</label><input id="cfg_sonarr_api_key" placeholder="API Key"></div>
<div class="btn-row"><button class="btn btn-primary" onclick="saveConfig()">💾 <span data-i18n="btn_save">Speichern</span></button><button class="btn btn-outline" onclick="testSingle('sonarr')">🧪 <span data-i18n="btn_test">Testen</span></button></div>
</div>

<div class="card"><h2><span class="tool-icon" id="tool-icon-radarr">🎬</span> Radarr</h2>
<div class="field"><label data-i18n="lbl_url">URL</label><input id="cfg_radarr_url" placeholder="http://your-nas:7878"></div>
<div class="field"><label data-i18n="lbl_api_key">API Key</label><input id="cfg_radarr_api_key" placeholder="API Key"></div>
<div class="btn-row"><button class="btn btn-primary" onclick="saveConfig()">💾 <span data-i18n="btn_save">Speichern</span></button><button class="btn btn-outline" onclick="testSingle('radarr')">🧪 <span data-i18n="btn_test">Testen</span></button></div>
</div>

<div class="card"><h2><span class="tool-icon" id="tool-icon-jackett">🌐</span> Jackett</h2>
<div class="field"><label data-i18n="lbl_url">URL</label><input id="cfg_jackett_url" placeholder="http://your-nas:9117"></div>
<div class="field"><label data-i18n="lbl_api_key">API Key</label><input id="cfg_jackett_api_key" placeholder="API Key"></div>
<div class="field"><label>Jackett Admin-Passwort</label><input id="cfg_jackett_admin_password" type="password" placeholder="Nur wenn das Jackett-Dashboard geschuetzt ist"><div class="hint">Das „Admin password“ aus Jackett. Für den automatischen Cookie-Eintrag erforderlich; sonst leer lassen.</div></div>
<div class="field"><label data-i18n="lbl_torznab_url">Torznab URL</label><input id="cfg_upstream_torznab_url" placeholder="http://..."></div>
<div class="btn-row"><button class="btn btn-primary" onclick="saveConfig()">💾 <span data-i18n="btn_save">Speichern</span></button><button class="btn btn-outline" onclick="testSingle('jackett')">🧪 <span data-i18n="btn_test">Testen</span></button></div>
</div>

<div class="card"><h2><span class="tool-icon" id="tool-icon-qbit">📥</span> qBittorrent</h2>
<div class="field"><label data-i18n="lbl_url">URL</label><input id="cfg_qbit_url" placeholder="http://your-nas:8080"></div>
<div class="field"><label data-i18n="lbl_username">Benutzername</label><input id="cfg_qbit_user" placeholder="admin"></div>
<div class="field"><label data-i18n="lbl_password">Passwort</label><input id="cfg_qbit_pass" type="password" data-i18n-placeholder="lbl_password"></div>
<div class="btn-row"><button class="btn btn-primary" onclick="saveConfig()">💾 <span data-i18n="btn_save">Speichern</span></button><button class="btn btn-outline" onclick="testSingle('qbit')">🧪 <span data-i18n="btn_test">Testen</span></button></div>
</div>

</div>

<!-- versteckte Felder für Bridge-Einstellungen (werden trotzdem gespeichert) -->
<input type="hidden" id="cfg_cache_ttl_seconds" value="300">
<input type="hidden" id="cfg_log_level" value="INFO">
</div>

<!-- INDEXER -->
<div class="panel" id="panel-indexer">
<div class="card"><h2>🔎 Indexer – TurkTorrent.us</h2>
<div class="hint" style="margin-bottom:12px">Der Login läuft halb-automatisch: <b>FlareSolverr</b> löst Cloudflare automatisch. Wenn ein <b>hCaptcha</b> erscheint, bekommst du einen <b>Telegram-Link</b> zum manuellen Lösen (dauert ~10 Sek).</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
<div>
<div class="field"><label>🤖 FlareSolverr URL</label><input id="cfg_flaresolverr_url" placeholder="http://localhost:8191"></div>
<div class="field"><label>🔗 Captcha-/Tailscale-URL</label><input id="cfg_bridge_external_url" placeholder="https://dein-nas.tailnet-name.ts.net"><div class="hint">Optional: die vom Handy erreichbare vollständige Bridge-URL. Sie wird für den Telegram-Captcha-Link verwendet.</div></div>
<div class="field"><label>👤 TurkTorrent Benutzername</label><input id="cfg_turktorrent_username" placeholder="dein TurkTorrent Username"></div>
<div class="field"><label>🔑 TurkTorrent Passwort</label><input id="cfg_turktorrent_password" type="password" placeholder="dein TurkTorrent Passwort"></div>
</div>
<div>
<div class="field"><label>🌐 TurkTorrent Site-URL</label><input id="cfg_turktorrent_site_url" placeholder="https://turktorrent.us"></div>
<div class="field"><label>🏷️ Jackett Indexer-ID</label><input id="cfg_turktorrent_jackett_indexer_id" placeholder="turktorrent"></div>
<div class="field"><label>⏱️ Refresh-Intervall (Minuten)</label><input id="cfg_turktorrent_cookie_interval_minutes" type="number" min="10" placeholder="120"></div>
<div class="field" style="margin-top:8px">
<label style="display:flex;align-items:center;gap:8px;cursor:pointer">
<input type="checkbox" id="cfg_turktorrent_cookie_auto_refresh" style="width:16px;height:16px;accent-color:var(--accent)">
<span style="font-size:.85rem">🔄 Auto-Cookie-Refresh aktiviert</span>
</label>
</div>
</div>
</div>

<div class="btn-row" style="margin-top:12px">
<button class="btn btn-primary" onclick="saveConfig()">💾 Speichern</button>
<button class="btn btn-outline" onclick="refreshCookieNow()">🍪 Cookie jetzt aktualisieren</button>
<button class="btn btn-outline" onclick="testFlareSolverr()">🤖 FlareSolverr testen</button>
<button class="btn btn-outline" onclick="testTurkTorrentLogin()">🧪 Login testen</button>
</div>

<div id="cookieStatus" style="margin-top:12px;padding:10px 14px;border-radius:8px;background:var(--bg);border:1px solid var(--border);font-size:.82rem;color:var(--text2)">Status wird geladen...</div>
</div>
</div>

<!-- EINSTELLUNGEN: TUNING -->
<div class="panel" id="panel-settings-tuning">
<div class="card"><h2>📦 <span data-i18n="tuning_title">BoxSet-Strategie</span></h2>
<div class="hint" style="margin-bottom:10px" data-i18n="tuning_hint">Wenn Sonarr keine einzelnen Staffeln findet, lädt die Bridge automatisch BoxSets direkt über qBittorrent.</div>
<div class="field" style="margin-bottom:8px">
<label style="display:flex;align-items:center;gap:8px;cursor:pointer">
<input type="checkbox" id="cfg_boxset_auto_download" style="width:16px;height:16px;accent-color:var(--accent)">
<span style="font-size:.85rem" data-i18n="tuning_auto">BoxSet Auto-Download aktiviert</span>
</label>
</div>
<div class="field">
<label style="font-size:.82rem;font-weight:600;margin-bottom:8px;display:block" data-i18n="tuning_prio_label">Auswahl-Priorität</label>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">

<label style="cursor:pointer;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;position:relative">
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:2px">
  <span style="font-weight:700;font-size:.85rem">🎬 <span data-i18n="tuning_quality">Qualität</span></span>
  <input type="radio" name="boxset_prio" value="quality" id="cfg_prio_quality" style="accent-color:var(--accent);width:15px;height:15px;flex-shrink:0">
</div>
<span style="font-size:.75rem;color:var(--text3);line-height:1.4" data-i18n="tuning_quality_desc">1080p vor 720p – auch ohne Seeder</span>
</label>

<label style="cursor:pointer;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;position:relative">
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:2px">
  <span style="font-weight:700;font-size:.85rem">🌱 <span data-i18n="tuning_seeders">Seeders</span></span>
  <input type="radio" name="boxset_prio" value="seeders" id="cfg_prio_seeders" style="accent-color:var(--accent);width:15px;height:15px;flex-shrink:0">
</div>
<span style="font-size:.75rem;color:var(--text3);line-height:1.4" data-i18n="tuning_seeders_desc">Meiste Seeders zuerst – schneller</span>
</label>

</div>
</div>
<div class="btn-row"><button class="btn btn-primary" onclick="saveConfig()">💾 <span data-i18n="btn_save">Speichern</span></button></div>
</div>
</div>

<!-- EINSTELLUNGEN: SYSTEM -->
<div class="panel" id="panel-settings-system">

<div class="card" style="margin-bottom:16px"><h2>🔒 <span data-i18n="sec_title">Sicherheit</span></h2>
<p style="font-size:.82rem;color:var(--text2);margin-bottom:16px" data-i18n="sec_desc">Schutzoptionen für das Bridge-Interface.</p>
<div class="field"><label data-i18n="sec_user">GUI Benutzername</label><input id="cfg_gui_user" data-i18n-placeholder="sec_user_ph" placeholder="Benutzername"></div>
<div class="btn-row" style="margin-top:4px"><button class="btn btn-primary" onclick="saveUsername()" data-i18n="sec_save_user">💾 Benutzername speichern</button></div>
<hr style="border:none;border-top:1px solid var(--border);margin:20px 0">
<p style="font-size:.85rem;font-weight:600;margin-bottom:12px" data-i18n="sec_pw_change">🔑 Passwort ändern</p>
<div class="field"><label data-i18n="sec_pass_old">Aktuelles Passwort</label><input id="pw_old" type="password" data-i18n-placeholder="sec_pass_old_ph" placeholder="Aktuelles Passwort"></div>
<div class="field"><label data-i18n="sec_pass_new">Neues Passwort</label><input id="pw_new" type="password" data-i18n-placeholder="sec_pass_new_ph" placeholder="Neues Passwort"></div>
<div class="field"><label data-i18n="sec_pass_new2">Neues Passwort (wiederholen)</label><input id="pw_new2" type="password" data-i18n-placeholder="sec_pass_new2_ph" placeholder="Neues Passwort wiederholen"></div>
<div id="pw_change_status" style="min-height:18px;font-size:.82rem;margin-bottom:8px"></div>
<div class="btn-row"><button class="btn btn-primary" onclick="changePassword()" data-i18n="sec_pw_btn">🔑 Passwort ändern</button></div>
</div>

<div class="card"><h2>💾 <span data-i18n="backup_title">Backup &amp; Restore</span></h2>
<p style="font-size:.82rem;color:var(--text2);margin-bottom:16px" data-i18n="backup_desc">Einstellungen sichern oder wiederherstellen. Die Backup-Datei enthält die komplette Konfiguration.</p>
<div style="display:flex;gap:12px;align-items:stretch">

<div style="flex:1;background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:16px;display:flex;flex-direction:column">
<div style="font-size:.88rem;font-weight:600;margin-bottom:6px" data-i18n="backup_dl_title">📥 Backup herunterladen</div>
<div class="hint" style="margin-bottom:0;flex:1" data-i18n="backup_dl_desc">Lädt die aktuelle Konfiguration als bridge_backup.json herunter.</div>
<div style="margin-top:14px"><button class="btn btn-outline" style="width:100%" onclick="downloadBackup()">📥 <span data-i18n="backup_dl_btn">Herunterladen</span></button></div>
</div>

<div style="flex:1;background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:16px;display:flex;flex-direction:column">
<div style="font-size:.88rem;font-weight:600;margin-bottom:6px" data-i18n="backup_ul_title">📤 Backup einspielen</div>
<div class="hint" style="margin-bottom:0;flex:1" data-i18n="backup_ul_desc">Wähle eine .json-Backup-Datei aus, um die Einstellungen wiederherzustellen.</div>
<input type="file" id="restoreFile" accept=".json" style="display:none" onchange="uploadRestore(this)">
<div style="margin-top:14px"><button class="btn btn-outline" style="width:100%" onclick="document.getElementById('restoreFile').click()">📤 <span data-i18n="backup_ul_btn">Einspielen</span></button></div>
</div>

</div>
<div id="restoreStatus" style="margin-top:10px;font-size:.82rem;min-height:0"></div>
</div>

</div>

<!-- NOTIFIKATIONEN: TELEGRAM -->
<div class="panel" id="panel-notif-telegram">
<div class="card"><h2>📨 <span data-i18n="tg_title">Telegram</span></h2>
<div class="field" style="margin-bottom:8px">
<label style="display:flex;align-items:center;gap:8px;cursor:pointer">
<input type="checkbox" id="cfg_telegram_enabled" style="width:16px;height:16px;accent-color:var(--accent)">
<span style="font-size:.85rem" data-i18n="tg_enabled">Benachrichtigungen aktiviert</span>
</label>
</div>
<div class="grid">
<div class="field"><label data-i18n="tg_token">Bot Token</label><input id="cfg_telegram_bot_token" placeholder="123456789:AABB..."><div class="hint" data-i18n="tg_token_hint">Erstellt über <b>@BotFather</b> in Telegram</div></div>
<div class="field"><label data-i18n="tg_chat">Chat ID</label><input id="cfg_telegram_chat_id" placeholder="-1001234567890"><div class="hint" data-i18n="tg_chat_hint">Chat-ID der Telegram-<b>Gruppe</b> oder des <b>Kanals</b>. Negative Zahl = Gruppe. Über <b>@userinfobot</b> ermitteln.</div></div>
</div>
<div class="btn-row"><button class="btn btn-primary" onclick="saveConfig()">💾 <span data-i18n="btn_save">Speichern</span></button><button class="btn btn-outline" onclick="testTelegram()">📨 <span data-i18n="tg_test">Test senden</span></button></div>
</div>
</div>

<!-- SUCHE -->
<div class="panel" id="panel-search">
<div class="card"><h2>🔍 <span data-i18n="search_title">Titel-Suche testen</span></h2>
<p style="font-size:.82rem;color:var(--text2);margin-bottom:14px" data-i18n="search_desc">Teste wie die Bridge einen Suchbegriff erweitert und was bei TürkTorrent gefunden wird.</p>
<div class="search-box">
<input id="searchInput" data-i18n-placeholder="search_ph" placeholder="z.B. Innocent, Deeply..." onkeydown="if(event.key==='Enter')runSearch()">
<button class="btn btn-primary" onclick="runSearch()">🔍 <span data-i18n="btn_search">Suchen</span></button>
</div>
<div id="searchStatus"></div>
<div id="searchVariants" style="margin-bottom:12px"></div>
<div id="searchResults"></div>
</div>
</div>

<!-- MAPPINGS -->
<div class="panel" id="panel-mappings">
<div class="card"><h2>📖 <span data-i18n="map_title">Titel-Mappings</span> <span id="mappingCount" style="font-size:.8rem;color:var(--text3)"></span></h2>
<p style="font-size:.82rem;color:var(--text2);margin-bottom:14px" data-i18n="map_desc">Alle bekannten Titel-Zuordnungen (int. Titel → türkische Varianten).</p>
<div class="search-box"><input id="mappingFilter" data-i18n-placeholder="map_filter" placeholder="Filter..." oninput="filterMappings()"></div>
<div class="btn-row" style="margin-bottom:14px">
<button class="btn btn-outline" onclick="refreshCache()">🔄 <span data-i18n="map_reload">Cache neu laden</span></button>
</div>
<div class="mapping-list" id="mappingList"></div>
</div>
</div>

<!-- GELERNT -->
<div class="panel" id="panel-learned">
<div class="card">
<h2>🧠 <span data-i18n="learned_title">Gelernte Titel-Mappings</span></h2>
<p style="color:var(--text3);margin-bottom:14px" data-i18n="learned_desc">Die Bridge lernt automatisch: Bei jedem unbekannten Suchbegriff fragt sie TVDB via Sonarr-Lookup ab und speichert alle Alternativtitel dauerhaft in <code>/config/learned_mappings.json</code>.</p>
<div class="btn-row" style="margin-bottom:14px">
<button class="btn" onclick="loadLearned()">🔄 <span data-i18n="btn_refresh">Aktualisieren</span></button>
<button class="btn btn-outline" onclick="clearAllLearned()" style="background:rgba(239,68,68,.15);border-color:#ef4444;color:#ef4444">🗑 <span data-i18n="btn_delete_all">Alle löschen</span></button>
</div>
<input id="learnedSearch" type="text" data-i18n-placeholder="map_filter" placeholder="Filter..." oninput="filterLearned()" style="width:100%;padding:9px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);margin-bottom:14px">
<div id="learnedTable" style="overflow-x:auto;max-height:480px;overflow-y:auto"></div>
</div>
</div>

<!-- LOGS -->
<div class="panel" id="panel-logs">
<div class="card"><h2>📋 <span data-i18n="logs_title">Bridge-Logs</span></h2>
<div class="btn-row" style="margin-bottom:14px">
<button class="btn btn-outline" onclick="fetchLogs()">🔄 <span data-i18n="btn_refresh">Aktualisieren</span></button>
<button class="btn btn-outline" onclick="clearLogs()">🗑 <span data-i18n="btn_clear">Leeren</span></button>
</div>
<div class="log-box" id="logBox" data-i18n="logs_empty">Klicke "Aktualisieren" um Logs zu laden...</div>
</div>
</div>

</div><!-- end main-content -->
</div><!-- end app-body -->

<script>
const ICON_URLS = {
    'Sonarr (TR)': 'data:image/png;base64,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',
    'Radarr (TR)': 'data:image/png;base64,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',
    'Jackett': 'data:image/png;base64,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',
    'qBittorrent': 'data:image/svg+xml;base64,PHN2ZyBoZWlnaHQ9IjEwMjQiIHZpZXdCb3g9IjAgMCAxMDI0IDEwMjQiIHdpZHRoPSIxMDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHhtbG5zOnhsaW5rPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5L3hsaW5rIj48bGluZWFyR3JhZGllbnQgaWQ9ImEiIGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIiB4MT0iMzQ4LjI4MjkiIHgyPSI3ODIuMDU5NTEiIHkxPSIwIiB5Mj0iNzg2LjQ4MzIyIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM3MmI0ZjUiLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMzNTZlYmYiLz48L2xpbmVhckdyYWRpZW50PjxnIGZpbGw9Im5vbmUiIGZpbGwtcnVsZT0iZXZlbm9kZCIgdHJhbnNmb3JtPSJtYXRyaXgoLjk3NjU2MjY4IDAgMCAuOTc2NTYyNCAxMS45OTk5MDggMTIuMDAwMDUxKSI+PGNpcmNsZSBjeD0iNTEyIiBjeT0iNTEyIiBmaWxsPSJ1cmwoI2EpIiByPSI0OTYiIHN0cm9rZT0iI2RhZWZmZiIgc3Ryb2tlLXdpZHRoPSIzMiIvPjxwYXRoIGQ9Im03MTIuODk4IDMzMi4zOTlxNjYuNjU3IDAgMTAzLjM4IDQ1LjY3MSAzNy4wMyA0NS4zNjQgMzcuMDMgMTI4LjY4NCAwIDgzLjMyLTM3LjM0IDEyOS42MS0zNy4wMyA0NS45OC0xMDMuMDcgNDUuOTgtMzMuMDIgMC02MC40ODQtMTIuMDM1LTI3LjE1Ni0xMi4zNDQtNDUuNjcyLTM3LjY0OWgtMy43MDNsLTEwLjggNDMuNTEyaC0zNi43MjR2LTQ4MC4xNzJoNTEuMjI3djExNi42NXEwIDM5LjE5MS0yLjQ2OSA3MC4zNTloMi40N3EzNS43OTYtNTAuNjEgMTA2LjE1NS01MC42MXptLTcuNDA2IDQyLjg5NHEtNTIuNDYgMC03NS42MDUgMzAuMjQyLTIzLjE0NSAyOS45MzQtMjMuMTQ1IDEwMS4yMTkgMCA3MS4yODUgMjMuNzYyIDEwMi4xNDUgMjMuNzYxIDMwLjU1IDc2LjIyMiAzMC41NSA0Ny4yMTUgMCA3MC4zNi0zNC4yNTQgMjMuMTQ0LTM0LjU2MiAyMy4xNDQtOTkuMDU4IDAtNjYuMDQtMjMuMTQ0LTk4LjQ0Mi0yMy4xNDUtMzIuNDAyLTcxLjU5NC0zMi40MDJ6IiBmaWxsPSIjZmZmIi8+PHBhdGggZD0ibTMxNy4yNzMgNjM5LjQ1cTUxLjIyNyAwIDc0LjY4LTI3LjQ2NiAyMy40NTMtMjcuNDY0IDI0Ljk5Ni05Mi41Nzh2LTExLjQxOHEwLTcwLjk3Ni0yNC4wNy0xMDIuMTQ0LTI0LjA3LTMxLjE2OC03Ni4yMjMtMzEuMTY4LTQ1LjA1NSAwLTY5LjEyNSAzNS4xOC0yMy43NjIgMzQuODctMjMuNzYyIDk4Ljc1IDAgNjMuODc5IDIzLjQ1NCA5Ny41MTUgMjMuNzYxIDMzLjMyOCA3MC4wNSAzMy4zMjh6bS03LjcxNSA0Mi44OTRxLTY1LjQyMSAwLTEwMi4xNDQtNDUuOTgtMzYuNzIzLTQ1Ljk4MS0zNi43MjMtMTI4LjM3NiAwLTgzLjAxMSAzNy4wMzItMTI5LjYwOSAzNy4wMy00Ni41OTggMTAzLjA3LTQ2LjU5OCA2OS40MzMgMCAxMDYuNzczIDUyLjQ2MWgyLjc3OGw3LjQwNi00Ni4yODloNDAuNDI2djQ5MC4wNDdoLTUxLjIyN3YtMTQ0LjczcTAtMzAuODYgMy4zOTUtNTIuNDYxaC00LjAxMnEtMzUuNDg4IDUxLjUzNS0xMDYuNzc0IDUxLjUzNXoiIGZpbGw9IiNjOGU4ZmYiLz48L2c+PC9zdmc+',
    'SABnzbd': 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgcng9IjE4IiBmaWxsPSIjRjVBNjIzIi8+PHRleHQgeD0iNTAiIHk9IjY4IiBmb250LXNpemU9IjUyIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiIgZm9udC13ZWlnaHQ9ImJvbGQiIGZpbGw9IiNmZmYiPlM8L3RleHQ+PC9zdmc+',
    'Plex': 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgcng9IjE4IiBmaWxsPSIjRTVBMDBEIi8+PHBvbHlnb24gcG9pbnRzPSIzMiwyMiAzMiw3OCA2OCw1MCIgZmlsbD0iI2ZmZiIvPjwvc3ZnPg==',
    'Jellyfin': 'data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0idXRmLTgiPz4KPCEtLSAqKioqKiBCRUdJTiBMSUNFTlNFIEJMT0NLICoqKioqCiAgLSBQYXJ0IG9mIHRoZSBKZWxseWZpbiBwcm9qZWN0IChodHRwczovL2plbGx5ZmluLm1lZGlhKQogIC0gCiAgLSBBbGwgY29weXJpZ2h0IGJlbG9uZ3MgdG8gdGhlIEplbGx5ZmluIGNvbnRyaWJ1dG9yczsgYSBmdWxsIGxpc3QgY2FuCiAgLSBiZSBmb3VuZCBpbiB0aGUgZmlsZSBDT05UUklCVVRPUlMubWQKICAtIAogIC0gVGhpcyB3b3JrIGlzIGxpY2Vuc2VkIHVuZGVyIHRoZSBDcmVhdGl2ZSBDb21tb25zIEF0dHJpYnV0aW9uLVNoYXJlQWxpa2UgNC4wIEludGVybmF0aW9uYWwgTGljZW5zZS4KICAtIFRvIHZpZXcgYSBjb3B5IG9mIHRoaXMgbGljZW5zZSwgdmlzaXQgaHR0cDovL2NyZWF0aXZlY29tbW9ucy5vcmcvbGljZW5zZXMvYnktc2EvNC4wLy4KLSAqKioqKiBFTkQgTElDRU5TRSBCTE9DSyAqKioqKiAtLT4KPHN2ZyB2ZXJzaW9uPSIxLjEiIGlkPSJpY29uLXRyYW5zcGFyZW50IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHhtbG5zOnhsaW5rPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5L3hsaW5rIiB2aWV3Qm94PSIwIDAgNTEyIDUxMiI+Cgk8ZGVmcz4KCQk8bGluZWFyR3JhZGllbnQgaWQ9ImxpbmVhci1ncmFkaWVudCIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiIHgxPSIxMTAuMjUiIHkxPSIyMTMuMyIgeDI9IjQ5Ni4xNCIgeTI9IjQzNi4wOSI+CgkJCTxzdG9wIG9mZnNldD0iMCIgc3R5bGU9InN0b3AtY29sb3I6I0FBNUNDMyIvPgoJCQk8c3RvcCBvZmZzZXQ9IjEiIHN0eWxlPSJzdG9wLWNvbG9yOiMwMEE0REMiLz4KCQk8L2xpbmVhckdyYWRpZW50PgoJPC9kZWZzPgoJPHRpdGxlPmljb24tdHJhbnNwYXJlbnQ8L3RpdGxlPgoJPGcgaWQ9Imljb24tdHJhbnNwYXJlbnQiPgoJCTxwYXRoIGlkPSJpbm5lci1zaGFwZSIgZD0iTTI1NiwyMDEuNmMtMjAuNCwwLTg2LjIsMTE5LjMtNzYuMiwxMzkuNHMxNDIuNSwxOS45LDE1Mi40LDBTMjc2LjUsMjAxLjYsMjU2LDIwMS42eiIgZmlsbD0idXJsKCNsaW5lYXItZ3JhZGllbnQpIi8+CgkJPHBhdGggaWQ9Im91dGVyLXNoYXBlIiBkPSJNMjU2LDIzLjNjLTYxLjYsMC0yNTkuOCwzNTkuNC0yMjkuNiw0MjAuMXM0MjkuMyw2MCw0NTkuMiwwUzMxNy42LDIzLjMsMjU2LDIzLjN6CgkJTTQwNi41LDM5MC44Yy0xOS42LDM5LjMtMjgxLjEsMzkuOC0zMDAuOSwwczExMC4xLTI3NS4zLDE1MC40LTI3NS4zUzQyNi4xLDM1MS40LDQwNi41LDM5MC44eiIgZmlsbD0idXJsKCNsaW5lYXItZ3JhZGllbnQpIi8+Cgk8L2c+Cjwvc3ZnPgo=',
    'Telegram Bot': 'data:image/png;base64,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',
    'TürkTorrent (Torznab)': 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgcng9IjE4IiBmaWxsPSIjYzAzOTJiIi8+PHRleHQgeD0iNTAiIHk9IjY1IiBmb250LXNpemU9IjM4IiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiIgZm9udC13ZWlnaHQ9ImJvbGQiIGZpbGw9IiNmZmYiPlRSPC90ZXh0Pjwvc3ZnPg==',
    'TurkTorrent': 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgcng9IjE4IiBmaWxsPSIjYzAzOTJiIi8+PHRleHQgeD0iNTAiIHk9IjY1IiBmb250LXNpemU9IjM4IiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiIgZm9udC13ZWlnaHQ9ImJvbGQiIGZpbGw9IiNmZmYiPlRSPC90ZXh0Pjwvc3ZnPg==',
};

const API = '';
let allMappings = {};

function toggleMusic() {
  const audio = document.getElementById('headerAudio');
  const btn = document.getElementById('audioBtn');
  if (audio.paused) {
    audio.volume = 0.3;
    audio.play().then(() => { btn.textContent = '⏸'; btn.classList.add('playing'); })
      .catch(() => toast('Audio konnte nicht abgespielt werden', 'err'));
  } else {
    audio.pause();
    btn.textContent = '▶';
    btn.classList.remove('playing');
  }
}

function toggleProfileDropdown(event) {
  event.stopPropagation();
  const dd = document.getElementById('profileDropdown');
  const btn = document.getElementById('profileBtn');
  if (dd.classList.contains('open')) {
    dd.classList.remove('open');
  } else {
    const rect = btn.getBoundingClientRect();
    dd.style.top = (rect.bottom + 8) + 'px';
    dd.style.right = (window.innerWidth - rect.right) + 'px';
    dd.classList.add('open');
  }
}
// Dropdown schließen bei Klick außerhalb – capture:true damit es vor onclick feuert
document.addEventListener('click', function(e) {
  const dd = document.getElementById('profileDropdown');
  const btn = document.getElementById('profileBtn');
  if (!dd || !btn) return;
  if (dd.classList.contains('open') && !dd.contains(e.target) && !btn.contains(e.target)) {
    dd.classList.remove('open');
  }
}, true);

async function loadVersion() {
  try {
    const h = await api('/gui/api/health');
    const v = h.version || '1.0.0';
    const el = document.getElementById('subVersion');
    if (el) el.textContent = 'v' + v;
  } catch(e) {}
}
loadVersion();

// ── Update-Check ──
let _updateDismissed = sessionStorage.getItem('updateDismissed') || '';
async function checkForUpdate() {
  try {
    const r = await api('/gui/api/update-check');
    const banner = document.getElementById('updateBanner');
    if (!banner) return;
    if (r.update_available && _updateDismissed !== r.remote_sha) {
      document.getElementById('ubMsg').textContent = r.message || 'Neue Version verfügbar!';
      document.getElementById('ubDate').textContent = r.remote_date ? ('Aktualisiert: ' + r.remote_date) : '';
      banner.style.display = 'block';
    } else {
      banner.style.display = 'none';
    }
  } catch(e) {}
}
function dismissUpdate() {
  const banner = document.getElementById('updateBanner');
  if (banner) banner.style.display = 'none';
  // Für diese Session merken
  api('/gui/api/update-check').then(r => {
    if (r.remote_sha) { _updateDismissed = r.remote_sha; sessionStorage.setItem('updateDismissed', r.remote_sha); }
  }).catch(()=>{});
}
checkForUpdate();
setInterval(checkForUpdate, 300000); // alle 5 Min

function toggleGroup(el) {
  el.classList.toggle('open');
  const items = el.nextElementSibling;
  if (items) items.classList.toggle('open');
}

function navTo(panel, el) {
  // Panels
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const target = document.getElementById('panel-' + panel);
  if (target) target.classList.add('active');
  // Sidebar items – alle Typen zurücksetzen
  document.querySelectorAll('.sidebar-item, .sidebar-dashboard').forEach(i => i.classList.remove('active'));
  if (el) el.classList.add('active');
  // Side effects
  if (panel === 'dashboard') loadDashboard();
  if (panel === 'mappings') loadMappings();
  if (panel === 'connections' || panel === 'settings-system' || panel === 'settings-tuning' || panel === 'notif-telegram' || panel === 'indexer') loadConfig();
  if (panel === 'connections') applyToolIcons();
  if (panel === 'indexer') loadIndexerStatus();
  if (panel === 'logs') fetchLogs();
  if (panel === 'learned') loadLearned();
}

function switchTab(name) { navTo(name, document.querySelector('[data-panel="'+name+'"]')); }

function applyToolIcons() {
  const MAP = {
    'tool-icon-sonarr': 'Sonarr (TR)',
    'tool-icon-radarr': 'Radarr (TR)',
    'tool-icon-jackett': 'Jackett',
    'tool-icon-qbit': 'qBittorrent',
  };
  for (const [id, name] of Object.entries(MAP)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const url = ICON_URLS[name];
    if (url) {
      el.innerHTML = '<img style="width:28px;height:28px;vertical-align:middle;border-radius:6px;margin-right:6px;object-fit:contain" src="' + url + '" alt="' + name + '">';
    }
  }
}

function toast(msg, type='ok') {
  const d = document.createElement('div');
  d.className = 'toast ' + type;
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 3000);
}

async function api(path, opts) {
  const r = await fetch(API + path, opts);
  return r.json();
}

// ── Indexer ──
async function loadIndexerStatus() {
  const el = document.getElementById('cookieStatus');
  if (el) {
    try {
      const r = await api('/gui/api/indexer-status');
      let html = '<strong>🤖 FlareSolverr:</strong> ' + (r.flaresolverr_ok ? '✅ Verbunden' : '❌ Nicht erreichbar') + ' <span style="opacity:.5">(' + esc(r.flaresolverr_url || '–') + ')</span>';
      html += '<br><strong>🍪 Cookie-Status:</strong> ' + esc(r.cookie_status || 'Noch kein Refresh');
      if (r.last_refresh) html += '<br><strong>⏰ Letzter Refresh:</strong> ' + esc(r.last_refresh);
      html += '<br><strong>🔄 Auto-Refresh:</strong> ' + (r.auto_refresh_enabled ? '✅ Aktiv' : '❌ Deaktiviert');
      if (r.next_refresh) html += '<br><strong>⏭️ Nächster Refresh:</strong> ' + esc(r.next_refresh);
      el.innerHTML = html;
    } catch(e) { el.textContent = '❌ Status konnte nicht geladen werden'; }
  }
}

async function refreshCookieNow() {
  try {
    toast('🍪 Cookie wird aktualisiert...', 'ok');
    const r = await api('/gui/api/refresh-cookie', {method:'POST', headers:{'Content-Type':'application/json'}});
    if (r.ok) {
      toast('✅ Cookie erfolgreich aktualisiert!', 'ok');
    } else {
      toast('❌ ' + (r.error || 'Fehler beim Cookie-Refresh'), 'err');
    }
    loadIndexerStatus();
    loadDashboardIndexerStatus();
  } catch(e) { toast('❌ ' + e.message, 'err'); }
}

async function testTurkTorrentLogin() {
  try {
    toast('🧪 Login starten... (Telegram-Captcha wird angefordert)', 'ok');
    const cfg = gatherConfig();
    const r = await api('/gui/api/test-turktorrent-login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username: cfg.turktorrent_username, password: cfg.turktorrent_password, site_url: cfg.turktorrent_site_url, flaresolverr_url: cfg.flaresolverr_url})
    });
    if (r.ok) {
      toast('✅ Login erfolgreich! Cookie: ' + (r.cookie || '').substring(0, 50) + '...', 'ok');
    } else {
      toast('❌ ' + (r.error || 'Login fehlgeschlagen'), 'err');
    }
  } catch(e) { toast('❌ ' + e.message, 'err'); }
}

async function testFlareSolverr() {
  try {
    toast('🤖 Teste FlareSolverr-Verbindung...', 'ok');
    const cfg = gatherConfig();
    const r = await api('/gui/api/test-flaresolverr', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({flaresolverr_url: cfg.flaresolverr_url})
    });
    if (r.ok) {
      toast('✅ FlareSolverr erreichbar! Version: ' + (r.version || '?'), 'ok');
    } else {
      toast('❌ ' + (r.error || 'FlareSolverr nicht erreichbar'), 'err');
    }
  } catch(e) { toast('❌ ' + e.message, 'err'); }
}

async function loadDashboardIndexerStatus() {
  const dot = document.getElementById('indexerStatusDot');
  const text = document.getElementById('indexerStatusText');
  const detail = document.getElementById('indexerStatusDetail');
  if (!dot) return;
  try {
    const r = await api('/gui/api/indexer-status');
    const isOk = r.cookie_status && r.cookie_status.startsWith('✅');
    dot.className = 'status-dot ' + (isOk ? 'ok' : 'err');
    text.textContent = isOk ? 'Cookie aktiv' : 'Cookie-Problem';
    detail.textContent = r.cookie_status || 'Unbekannt';
  } catch(e) {
    dot.className = 'status-dot err';
    text.textContent = 'Fehler';
    detail.textContent = e.message;
  }
}

// ── Dashboard ──
async function loadDashboard() {
  try {
    testAllConnections();
    loadDashboardIndexerStatus();
  } catch(e) { console.error(e); }
}

async function testAllConnections() {
  const grid = document.getElementById('connGrid');
  grid.innerHTML = '<div style="color:var(--text3)"><span class="spinner"></span> Teste Verbindungen...</div>';
  try {
    const r = await api('/gui/api/test-connections');
    let html = '';
    for (const c of r.results) {
      const ok = c.status === 'ok';
      const dotClass = ok ? 'ok' : 'err';
      const icon = ICON_URLS[c.name];
      const iconHtml = icon
        ? `<img class="app-icon-img" src="${icon}" alt="${esc(c.name)}">`
        : `<span class="app-icon">🔌</span>`;
      html += `<div class="conn-card ${dotClass}">
        ${iconHtml}
        <div class="name">${esc(c.name)}</div>
        <div class="status-row">
          <div class="status-dot ${dotClass}"></div>
          <span class="status-text">${ok ? 'Online' : 'Fehler'}</span>
        </div>
        <div class="url">${esc(c.url)}</div>
        <div class="detail">${esc(c.detail)}</div>
      </div>`;
    }
    grid.innerHTML = html;
  } catch(e) { grid.innerHTML = '<div class="status err">Fehler: ' + esc(e.message) + '</div>'; }
}

async function testSingle(service) {
  try {
    toast('Teste ' + service + '...', 'ok');
    const cfg = gatherConfig();
    const r = await api('/gui/api/test-single', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({service, ...cfg})});
    if (r.status === 'ok') {
      toast(service + ': ' + r.detail, 'ok');
      const inp = document.getElementById('cfg_' + service + '_url');
      if (inp) { inp.classList.remove('err'); inp.classList.add('ok'); }
    } else {
      toast(service + ': ' + r.detail, 'err');
      const inp = document.getElementById('cfg_' + service + '_url');
      if (inp) { inp.classList.remove('ok'); inp.classList.add('err'); }
    }
  } catch(e) { toast('Fehler: ' + e.message, 'err'); }
}

// ── Config ──
async function loadConfig() {
  try {
    const cfg = await api('/gui/api/config');
    for (const [k, v] of Object.entries(cfg)) {
      const el = document.getElementById('cfg_' + k);
      if (!el) continue;
      if (el.type === 'checkbox') {
        el.checked = !!v;
      } else {
        el.value = v ?? '';
      }
    }
    // Radio-Buttons für BoxSet-Prio
    if (cfg.boxset_prefer_seeders) {
      document.getElementById('cfg_prio_seeders').checked = true;
    } else {
      document.getElementById('cfg_prio_quality').checked = true;
    }
    // Benutzername im Dropdown anzeigen
    const uname = cfg.gui_user && cfg.gui_user.trim() ? cfg.gui_user.trim() : 'Admin';
    document.getElementById('ddUsername').textContent = uname;
  } catch(e) { toast('Config laden fehlgeschlagen', 'err'); }
}

function gatherConfig() {
  const cfg = {};
  document.querySelectorAll('[id^="cfg_"]').forEach(el => {
    const key = el.id.replace('cfg_', '');
    if (el.type === 'checkbox') {
      cfg[key] = el.checked;
    } else if (el.type === 'radio') {
      // Skip radios, handle separately
    } else if (el.type === 'number') {
      cfg[key] = Number(el.value);
    } else {
      cfg[key] = el.value;
    }
  });
  // BoxSet-Prio Radio
  cfg.boxset_prefer_seeders = document.getElementById('cfg_prio_seeders')?.checked || false;
  return cfg;
}

async function testTelegram() {
  try {
    const cfg = gatherConfig();
    toast('Sende Test-Nachricht...', 'ok');
    const r = await api('/gui/api/test-telegram', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: cfg.telegram_bot_token, chat_id: cfg.telegram_chat_id})
    });
    if (r.ok) toast('\u2705 Telegram-Nachricht gesendet!', 'ok');
    else toast('\u274c ' + (r.error || 'Fehler'), 'err');
  } catch(e) { toast('Fehler: ' + e.message, 'err'); }
}

function downloadBackup() {
  window.location.href = '/gui/api/backup';
}

async function uploadRestore(input) {
  const file = input.files[0];
  if (!file) return;
  const statusEl = document.getElementById('restoreStatus');
  statusEl.textContent = '⏳ Wird eingespielt...';
  statusEl.style.color = 'var(--text2)';
  try {
    const text = await file.text();
    let data;
    try { data = JSON.parse(text); } catch(e) {
      statusEl.textContent = '❌ Ungültige JSON-Datei!';
      statusEl.style.color = '#e74c3c';
      input.value = '';
      return;
    }
    const r = await api('/gui/api/restore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (r.ok) {
      statusEl.textContent = '✅ Erfolgreich wiederhergestellt!';
      statusEl.style.color = '#2ecc71';
      toast('✅ Konfiguration wiederhergestellt!', 'ok');
      loadConfig();
    } else {
      statusEl.textContent = '❌ Fehler: ' + (r.error || 'Unbekannt');
      statusEl.style.color = '#e74c3c';
    }
  } catch(e) {
    statusEl.textContent = '❌ ' + e.message;
    statusEl.style.color = '#e74c3c';
  }
  input.value = '';
}

async function saveConfig() {
  try {
    const cfg = gatherConfig();
    // gui_pass nie über saveConfig() senden – hat eigenes Formular
    delete cfg.gui_pass;
    const r = await api('/gui/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)});
    if (r.ok) toast('✓ Konfiguration gespeichert!', 'ok');
    else toast('Fehler: ' + (r.error || 'unbekannt'), 'err');
  } catch(e) { toast('Speichern fehlgeschlagen: ' + e.message, 'err'); }
}

async function saveUsername() {
  const t = LANGS[_lang] || LANGS.de;
  const user = (document.getElementById('cfg_gui_user')?.value || '').trim();
  if (!user) { toast(t.sec_user_err, 'err'); return; }
  try {
    const r = await api('/gui/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({gui_user: user})});
    if (r.ok) {
      toast(t.sec_user_ok, 'ok');
      document.getElementById('ddUsername').textContent = user;
    } else toast('Fehler: ' + (r.error || 'unbekannt'), 'err');
  } catch(e) { toast('Fehler: ' + e.message, 'err'); }
}

async function changePassword() {
  const t = LANGS[_lang] || LANGS.de;
  const statusEl = document.getElementById('pw_change_status');
  const oldPw  = (document.getElementById('pw_old')?.value  || '');
  const newPw  = (document.getElementById('pw_new')?.value  || '');
  const newPw2 = (document.getElementById('pw_new2')?.value || '');
  statusEl.textContent = '';
  statusEl.style.color = 'var(--text3)';
  if (!oldPw || !newPw || !newPw2) {
    statusEl.textContent = t.sec_pw_err_fill;
    statusEl.style.color = '#e74c3c';
    return;
  }
  if (!newPw.trim()) {
    statusEl.textContent = t.sec_pw_err_empty;
    statusEl.style.color = '#e74c3c';
    return;
  }
  if (newPw !== newPw2) {
    statusEl.textContent = t.sec_pw_err_match;
    statusEl.style.color = '#e74c3c';
    return;
  }
  try {
    const r = await api('/gui/api/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({old_pass: oldPw, new_pass: newPw})
    });
    if (r.ok) {
      statusEl.textContent = t.sec_pw_ok;
      statusEl.style.color = '#2ecc71';
      document.getElementById('pw_old').value = '';
      document.getElementById('pw_new').value = '';
      document.getElementById('pw_new2').value = '';
      toast(t.sec_pw_ok, 'ok');
    } else {
      const msg = r.error === 'wrong_password' ? t.sec_pw_err_old
                : r.error === 'empty'           ? t.sec_pw_err_empty
                : (r.error || 'Fehler');
      statusEl.textContent = msg;
      statusEl.style.color = '#e74c3c';
    }
  } catch(e) {
    statusEl.textContent = '❌ ' + e.message;
    statusEl.style.color = '#e74c3c';
  }
}

// ── Search ──
async function runSearch() {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) return;
  document.getElementById('searchStatus').innerHTML = '<span class="spinner"></span> Suche...';
  document.getElementById('searchVariants').innerHTML = '';
  document.getElementById('searchResults').innerHTML = '';
  try {
    const r = await api('/gui/api/search?q=' + encodeURIComponent(q));
    document.getElementById('searchStatus').innerHTML =
      `<span class="status ok">${r.total_results} Ergebnis(se) gefunden</span>`;
    let vh = '<div style="margin-bottom:8px;font-size:.8rem;color:var(--text2)">Suchvarianten: ';
    for (const v of (r.search_variants||[])) vh += `<span class="status ok" style="margin:2px">${esc(v)}</span> `;
    vh += '</div>';
    document.getElementById('searchVariants').innerHTML = vh;
    let rh = '';
    for (const [src, items] of Object.entries(r.results_by_source||{})) {
      rh += `<div style="padding:8px 0;font-size:.8rem;font-weight:600;color:var(--accent2)">${esc(src)} (${items.length})</div>`;
      for (const it of items.slice(0, 30)) {
        rh += `<div class="result-item">${esc(it.title||it.error||'?')}</div>`;
      }
    }
    document.getElementById('searchResults').innerHTML = rh || '<div style="color:var(--text3)">Keine Ergebnisse</div>';
  } catch(e) {
    document.getElementById('searchStatus').innerHTML = '<span class="status err">Fehler: ' + esc(e.message) + '</span>';
  }
}

// ── Mappings ──
async function loadMappings() {
  try {
    const r = await api('/gui/api/mappings');
    allMappings = r.mappings || {};
    document.getElementById('mappingCount').textContent = '(' + (r.count||0) + ' Einträge)';
    renderMappings(allMappings);
  } catch(e) { toast('Mappings laden fehlgeschlagen', 'err'); }
}

function renderMappings(m) {
  const list = document.getElementById('mappingList');
  const keys = Object.keys(m).sort();
  let html = '';
  for (const k of keys) {
    const vals = m[k];
    if (!Array.isArray(vals)) continue;
    html += `<div class="mapping-item"><span class="key">${esc(k)}</span><span class="vals">${vals.map(v=>esc(v)).join(', ')}</span></div>`;
  }
  list.innerHTML = html || '<div style="padding:12px;color:var(--text3)">Keine Mappings geladen</div>';
}

function filterMappings() {
  const f = document.getElementById('mappingFilter').value.toLowerCase();
  if (!f) return renderMappings(allMappings);
  const filtered = {};
  for (const [k, v] of Object.entries(allMappings)) {
    if (k.includes(f) || (Array.isArray(v) && v.some(x => x.toLowerCase().includes(f)))) filtered[k] = v;
  }
  renderMappings(filtered);
}

async function refreshCache() {
  toast('Cache wird neu geladen...', 'ok');
  try {
    const r = await api('/gui/api/refresh-cache', {method:'POST'});
    toast('✓ Cache aktualisiert: ' + (r.entries||'?') + ' Einträge', 'ok');
    loadMappings();
  } catch(e) { toast('Fehler: ' + e.message, 'err'); }
}

// ── Gelernte Mappings ──
let _learnedData = [];

async function loadLearned() {
  document.getElementById('learnedTable').innerHTML = '<span class="spinner"></span> Lade...';
  try {
    const r = await api('/gui/api/learned');
    _learnedData = r.learned || [];
    renderLearned(_learnedData);
  } catch(e) {
    document.getElementById('learnedTable').innerHTML = '<span style="color:#ef4444">Fehler: ' + esc(e.message) + '</span>';
  }
}

function renderLearned(data) {
  const div = document.getElementById('learnedTable');
  if (!data.length) { div.innerHTML = '<p style="color:var(--text3)">Noch keine gelernten Mappings. Starte Suchen über Sonarr – die Bridge lernt automatisch.</p>'; return; }
  let html = '<table style="width:100%;border-collapse:collapse;font-size:13px">';
  html += '<thead><tr style="border-bottom:1px solid var(--border);color:var(--text3)">';
  html += '<th style="text-align:left;padding:6px 8px">Suchwort</th><th style="text-align:left;padding:6px 8px">Gefundene Varianten</th><th style="padding:6px 8px">🔍</th><th style="padding:6px 8px">Quelle</th><th style="padding:6px 8px">Gelernt am</th><th style="padding:6px 8px"></th>';
  html += '</tr></thead><tbody>';
  for (const e of data) {
    const ts = e.learned_at ? e.learned_at.replace('T',' ').slice(0,16) : '';
    html += `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:6px 8px;font-weight:600;color:#a78bfa">${esc(e.key)}</td>
      <td style="padding:6px 8px">${e.titles.map(t=>`<span style="background:var(--bg3);border-radius:4px;padding:2px 6px;margin:1px;display:inline-block">${esc(t)}</span>`).join('')}</td>
      <td style="padding:6px 8px;text-align:center;color:var(--text3)">${e.search_count||0}</td>
      <td style="padding:6px 8px;text-align:center;color:var(--text3)">${esc(e.source||'?')}</td>
      <td style="padding:6px 8px;text-align:center;color:var(--text3);white-space:nowrap">${ts}</td>
      <td style="padding:6px 8px;text-align:center"><button class="btn btn-outline" style="padding:3px 8px;font-size:11px;color:#ef4444;border-color:#ef4444" onclick="deleteLearned('${esc(e.key)}')">✕</button></td>
    </tr>`;
  }
  html += '</tbody></table>';
  div.innerHTML = html;
}

function filterLearned() {
  const q = document.getElementById('learnedSearch').value.toLowerCase();
  renderLearned(q ? _learnedData.filter(e => e.key.includes(q) || e.titles.some(t=>t.toLowerCase().includes(q))) : _learnedData);
}

async function deleteLearned(key) {
  if (!confirm('Eintrag "' + key + '" wirklich löschen?')) return;
  try {
    const r = await api('/gui/api/learned/' + encodeURIComponent(key), {method:'DELETE'});
    if (r.ok) { toast('Gelöscht: ' + key, 'ok'); loadLearned(); loadDashboard(); }
    else toast('Fehler: ' + r.error, 'err');
  } catch(e) { toast('Fehler: ' + e.message, 'err'); }
}

async function clearAllLearned() {
  if (!confirm('ALLE gelernten Mappings löschen? Diese Aktion kann nicht rückgängig gemacht werden.')) return;
  try {
    const r = await api('/gui/api/learned', {method:'DELETE'});
    if (r.ok) { toast('Alle gelernten Mappings gelöscht', 'ok'); loadLearned(); loadDashboard(); }
    else toast('Fehler', 'err');
  } catch(e) { toast('Fehler: ' + e.message, 'err'); }
}

// ── Logs ──
async function fetchLogs() {
  try {
    const r = await api('/gui/api/logs');
    document.getElementById('logBox').textContent = r.logs || 'Keine Logs verfügbar';
    const box = document.getElementById('logBox');
    box.scrollTop = box.scrollHeight;
  } catch(e) { document.getElementById('logBox').textContent = 'Fehler: ' + e.message; }
}
function clearLogs() {
  document.getElementById('logBox').textContent = '(gelöscht)';
}

function esc(s) { const d=document.createElement('div');d.textContent=s||'';return d.innerHTML; }

// ── i18n ──
const LANGS = {
  de: {
    header_sub: 'Torznab Proxy für türkische Serien & Filme',
    nav_dashboard: 'Dashboard', nav_connections: 'Verbindungen', nav_tools: 'Tools',
    nav_settings: 'Einstellungen', nav_tuning: 'Tuning', nav_system: 'System',
    nav_notifications: 'Notifikationen', nav_telegram: 'Telegram',
    nav_info: 'Info', nav_search: 'Suche testen', nav_mappings: 'Titel-Mappings',
    nav_learned: 'Gelernt', nav_logs: 'Logs',
    stat_mappings: 'Titel-Mappings', stat_learned: '\ud83e\udde0 Gelernt',
    stat_series: 'Serien (Sonarr)', stat_movies: 'Filme (Radarr)', stat_uptime: 'Uptime',
    dash_connections: 'Verbindungen', btn_test_all: 'Alle testen',
    btn_logout: 'Abmelden', sub_online: 'Online',
    lbl_url: 'URL', lbl_api_key: 'API Key', lbl_torznab_url: 'Torznab URL',
    lbl_username: 'Benutzername', lbl_password: 'Passwort',
    btn_save: 'Speichern', btn_test: 'Testen', btn_search: 'Suchen',
    btn_refresh: 'Aktualisieren', btn_delete_all: 'Alle löschen', btn_clear: 'Leeren',
    tuning_title: 'BoxSet-Strategie',
    tuning_hint: 'Wenn Sonarr keine einzelnen Staffeln findet, lädt die Bridge automatisch BoxSets direkt über qBittorrent.',
    tuning_auto: 'BoxSet Auto-Download aktiviert',
    tuning_prio_label: 'Auswahl-Priorität',
    tuning_quality: 'Qualität', tuning_quality_desc: '1080p vor 720p – auch ohne Seeder',
    tuning_seeders: 'Seeders', tuning_seeders_desc: 'Meiste Seeders zuerst – schneller',
    sec_title: 'Sicherheit', sec_desc: 'Schutzoptionen für das Bridge-Interface.',
    sec_user: 'GUI Benutzername', sec_user_ph: 'Benutzername',
    sec_save_user: '💾 Benutzername speichern',
    sec_pw_change: '🔑 Passwort ändern',
    sec_pass_old: 'Aktuelles Passwort', sec_pass_old_ph: 'Aktuelles Passwort',
    sec_pass_new: 'Neues Passwort', sec_pass_new_ph: 'Neues Passwort',
    sec_pass_new2: 'Neues Passwort (wiederholen)', sec_pass_new2_ph: 'Neues Passwort wiederholen',
    sec_pw_btn: '🔑 Passwort ändern',
    sec_pw_ok: '✅ Passwort erfolgreich geändert!',
    sec_pw_err_old: '❌ Aktuelles Passwort ist falsch.',
    sec_pw_err_match: '❌ Neue Passwörter stimmen nicht überein.',
    sec_pw_err_empty: '❌ Passwort darf nicht leer sein.',
    sec_pw_err_fill: '❌ Bitte alle Passwort-Felder ausfüllen.',
    sec_user_ok: '✅ Benutzername gespeichert!',
    sec_user_err: '❌ Benutzername darf nicht leer sein.',
    sec_user_err_nopw: '❌ Erst muss ein Passwort gesetzt sein.',
    sec_user_empty_pw: '❌ Benutzername + Passwort müssen gesetzt sein.',
    sec_pw_err_admin: '❌ Aktuelles Passwort darf nicht leer sein.',
    backup_title: 'Backup & Restore',
    backup_desc: 'Einstellungen sichern oder wiederherstellen. Die Backup-Datei enthält die komplette Konfiguration.',
    backup_dl_title: '\ud83d\udce5 Backup herunterladen', backup_dl_desc: 'Lädt die aktuelle Konfiguration als bridge_backup.json herunter.',
    backup_dl_btn: 'Herunterladen',
    backup_ul_title: '\ud83d\udce4 Backup einspielen', backup_ul_desc: 'Wähle eine .json-Backup-Datei aus, um die Einstellungen wiederherzustellen.',
    backup_ul_btn: 'Einspielen',
    tg_title: 'Telegram', tg_enabled: 'Benachrichtigungen aktiviert',
    tg_token: 'Bot Token', tg_token_hint: 'Erstellt über @BotFather in Telegram',
    tg_chat: 'Chat ID', tg_chat_hint: 'Chat-ID der Telegram-Gruppe oder des Kanals. Negative Zahl = Gruppe.',
    tg_test: 'Test senden',
    search_title: 'Titel-Suche testen',
    search_desc: 'Teste wie die Bridge einen Suchbegriff erweitert und was bei TürkTorrent gefunden wird.',
    search_ph: 'z.B. Innocent, Deeply, Resurrection Ertugrul...',
    map_title: 'Titel-Mappings', map_desc: 'Alle bekannten Titel-Zuordnungen (int. Titel → türkische Varianten).',
    map_filter: 'Filter...', map_reload: 'Cache neu laden',
    learned_title: 'Gelernte Titel-Mappings',
    learned_desc: 'Die Bridge lernt automatisch: Bei jedem unbekannten Suchbegriff fragt sie TVDB via Sonarr-Lookup ab und speichert alle Alternativtitel dauerhaft.',
    logs_title: 'Bridge-Logs', logs_empty: 'Klicke "Aktualisieren" um Logs zu laden...',
  },
  en: {
    header_sub: 'Torznab Proxy for Turkish Series & Movies',
    nav_dashboard: 'Dashboard', nav_connections: 'Connections', nav_tools: 'Tools',
    nav_settings: 'Settings', nav_tuning: 'Tuning', nav_system: 'System',
    nav_notifications: 'Notifications', nav_telegram: 'Telegram',
    nav_info: 'Info', nav_search: 'Test Search', nav_mappings: 'Title Mappings',
    nav_learned: 'Learned', nav_logs: 'Logs',
    stat_mappings: 'Title Mappings', stat_learned: '\ud83e\udde0 Learned',
    stat_series: 'Series (Sonarr)', stat_movies: 'Movies (Radarr)', stat_uptime: 'Uptime',
    dash_connections: 'Connections', btn_test_all: 'Test All',
    btn_logout: 'Logout', sub_online: 'Online',
    lbl_url: 'URL', lbl_api_key: 'API Key', lbl_torznab_url: 'Torznab URL',
    lbl_username: 'Username', lbl_password: 'Password',
    btn_save: 'Save', btn_test: 'Test', btn_search: 'Search',
    btn_refresh: 'Refresh', btn_delete_all: 'Delete All', btn_clear: 'Clear',
    tuning_title: 'BoxSet Strategy',
    tuning_hint: 'If Sonarr finds no individual seasons, the bridge automatically downloads BoxSets via qBittorrent.',
    tuning_auto: 'BoxSet Auto-Download enabled',
    tuning_prio_label: 'Selection Priority',
    tuning_quality: 'Quality', tuning_quality_desc: '1080p before 720p – even without seeders',
    tuning_seeders: 'Seeders', tuning_seeders_desc: 'Most seeders first – faster',
    sec_title: 'Security', sec_desc: 'Protection options for the Bridge interface.',
    sec_user: 'GUI Username', sec_user_ph: 'Username',
    sec_save_user: '💾 Save Username',
    sec_pw_change: '🔑 Change Password',
    sec_pass_old: 'Current Password', sec_pass_old_ph: 'Current Password',
    sec_pass_new: 'New Password', sec_pass_new_ph: 'New Password',
    sec_pass_new2: 'Repeat New Password', sec_pass_new2_ph: 'Repeat new password',
    sec_pw_btn: '🔑 Change Password',
    sec_pw_ok: '✅ Password changed successfully!',
    sec_pw_err_old: '❌ Current password is wrong.',
    sec_pw_err_match: '❌ New passwords do not match.',
    sec_pw_err_empty: '❌ Password must not be empty.',
    sec_pw_err_fill: '❌ Please fill in all password fields.',
    sec_user_ok: '✅ Username saved!',
    sec_user_err: '❌ Username must not be empty.',
    sec_user_err_nopw: '❌ A password must be set first.',
    sec_user_empty_pw: '❌ Username and password must be set.',
    sec_pw_err_admin: '❌ Current password must not be empty.',
    backup_title: 'Backup & Restore',
    backup_desc: 'Save or restore settings. The backup file contains the complete configuration.',
    backup_dl_title: '\ud83d\udce5 Download Backup', backup_dl_desc: 'Downloads the current configuration as bridge_backup.json.',
    backup_dl_btn: 'Download',
    backup_ul_title: '\ud83d\udce4 Restore Backup', backup_ul_desc: 'Select a .json backup file to restore settings.',
    backup_ul_btn: 'Restore',
    tg_title: 'Telegram', tg_enabled: 'Notifications enabled',
    tg_token: 'Bot Token', tg_token_hint: 'Created via @BotFather in Telegram',
    tg_chat: 'Chat ID', tg_chat_hint: 'Chat ID of the Telegram group or channel. Negative number = group.',
    tg_test: 'Send Test',
    search_title: 'Test Title Search',
    search_desc: 'Test how the bridge expands a search term and what is found on TürkTorrent.',
    search_ph: 'e.g. Innocent, Deeply, Resurrection Ertugrul...',
    map_title: 'Title Mappings', map_desc: 'All known title associations (int. title → Turkish variants).',
    map_filter: 'Filter...', map_reload: 'Reload Cache',
    learned_title: 'Learned Title Mappings',
    learned_desc: 'The bridge learns automatically: for each unknown search term it queries TVDB via Sonarr lookup and saves all alternate titles permanently.',
    logs_title: 'Bridge Logs', logs_empty: 'Click "Refresh" to load logs...',
  },
  tr: {
    header_sub: 'Türk Dizileri ve Filmleri için Torznab Proxy',
    nav_dashboard: 'Panel', nav_connections: 'Bağlantılar', nav_tools: 'Araçlar',
    nav_settings: 'Ayarlar', nav_tuning: 'Ayarlama', nav_system: 'Sistem',
    nav_notifications: 'Bildirimler', nav_telegram: 'Telegram',
    nav_info: 'Bilgi', nav_search: 'Arama Testi', nav_mappings: 'Başlık Eşleşmeleri',
    nav_learned: 'Öğrenilen', nav_logs: 'Kayıtlar',
    stat_mappings: 'Başlık Eşleşmeleri', stat_learned: '\ud83e\udde0 Öğrenilen',
    stat_series: 'Diziler (Sonarr)', stat_movies: 'Filmler (Radarr)', stat_uptime: 'Çalışma Süresi',
    dash_connections: 'Bağlantılar', btn_test_all: 'Tümünü Test Et',
    btn_logout: 'Çıkış Yap', sub_online: 'Çevrimiçi',
    lbl_url: 'URL', lbl_api_key: 'API Anahtarı', lbl_torznab_url: 'Torznab URL',
    lbl_username: 'Kullanıcı Adı', lbl_password: 'Şifre',
    btn_save: 'Kaydet', btn_test: 'Test Et', btn_search: 'Ara',
    btn_refresh: 'Yenile', btn_delete_all: 'Tümünü Sil', btn_clear: 'Temizle',
    tuning_title: 'BoxSet Stratejisi',
    tuning_hint: 'Sonarr tek sezon bulamazsa, Bridge qBittorrent aracılığıyla otomatik olarak BoxSet indirir.',
    tuning_auto: 'BoxSet Otomatik İndirme aktif',
    tuning_prio_label: 'Seçim Önceliği',
    tuning_quality: 'Kalite', tuning_quality_desc: '1080p, 720p’den önce – seed olmasa da',
    tuning_seeders: 'Seederlar', tuning_seeders_desc: 'Önce en fazla seeder – daha hızlı',
    sec_title: 'Güvenlik', sec_desc: 'Bridge arayüzü için koruma seçenekleri.',
    sec_user: 'GUI Kullanıcı Adı', sec_user_ph: 'Kullanıcı Adı',
    sec_save_user: '💾 Kullanıcı Adını Kaydet',
    sec_pw_change: '🔑 Şifre Değiştir',
    sec_pass_old: 'Mevcut Şifre', sec_pass_old_ph: 'Mevcut Şifre',
    sec_pass_new: 'Yeni Şifre', sec_pass_new_ph: 'Yeni Şifre',
    sec_pass_new2: 'Yeni Şifre (tekrar)', sec_pass_new2_ph: 'Yeni şifreyi tekrarla',
    sec_pw_btn: '🔑 Şifreyi Değiştir',
    sec_pw_ok: '✅ Şifre başarıyla değiştirildi!',
    sec_pw_err_old: '❌ Mevcut şifre yanlış.',
    sec_pw_err_match: '❌ Yeni şifreler eşleşmiyor.',
    sec_pw_err_empty: '❌ Şifre boş olamaz.',
    sec_pw_err_fill: '❌ Lütfen tüm şifre alanlarını doldurun.',
    sec_user_ok: '✅ Kullanıcı adı kaydedildi!',
    sec_user_err: '❌ Kullanıcı adı boş olamaz.',
    sec_user_err_nopw: '❌ Önce bir şifre belirlenmelidir.',
    sec_user_empty_pw: '❌ Kullanıcı adı ve şifre belirlenmelidir.',
    sec_pw_err_admin: '❌ Mevcut şifre boş olamaz.',
    backup_title: 'Yedekleme & Geri Yükleme',
    backup_desc: 'Ayarları kaydet veya geri yükle. Yedek dosyası tüm konfigürasyonı içerir.',
    backup_dl_title: '\ud83d\udce5 Yedeği İndir', backup_dl_desc: 'Mevcut konfigürasyonı bridge_backup.json olarak indirir.',
    backup_dl_btn: 'İndir',
    backup_ul_title: '\ud83d\udce4 Yedeği Yükle', backup_ul_desc: 'Ayarları geri yüklemek için bir .json yedek dosyası seçin.',
    backup_ul_btn: 'Yükle',
    tg_title: 'Telegram', tg_enabled: 'Bildirimler aktif',
    tg_token: 'Bot Token', tg_token_hint: 'Telegram’da @BotFather aracılığıyla oluşturulur',
    tg_chat: 'Chat ID', tg_chat_hint: 'Telegram grubu veya kanalının Chat ID’si. Negatif sayı = grup.',
    tg_test: 'Test Gönder',
    search_title: 'Başlık Arama Testi',
    search_desc: 'Bridge’nin arama terimini nasıl genişlettiğini ve TürkTorrent’te ne bulunduğunu test et.',
    search_ph: 'ör. Innocent, Deeply, Diriliş Ertuğrul...',
    map_title: 'Başlık Eşleşmeleri', map_desc: 'Tüm bilinen başlık eşleşmeleri (ulusl. başlık → Türkçe varyantlar).',
    map_filter: 'Filtrele...', map_reload: 'Cache’yi Yenile',
    learned_title: 'Öğrenilen Başlık Eşleşmeleri',
    learned_desc: 'Bridge otomatik olarak öğrenir: Her bilinmeyen arama terimi için Sonarr üzerinden TVDB’yi sorgular.',
    logs_title: 'Bridge Kayıtları', logs_empty: 'Kayıtları yüklemek için "Yenile"ye tıklayın...',
  }
};

let _lang = localStorage.getItem('ui_lang') || 'tr';

function setLang(lang) {
  _lang = lang;
  localStorage.setItem('ui_lang', lang);
  // Buttons updaten
  document.querySelectorAll('.lang-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('lang-' + lang);
  if (btn) btn.classList.add('active');
  // Alle data-i18n Elemente updaten
  const t = LANGS[lang] || LANGS.de;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (t[key] !== undefined) el.textContent = t[key];
  });
  // Placeholder updaten
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    if (t[key] !== undefined) el.placeholder = t[key];
  });
}

// Initial
loadDashboard();
setLang(_lang);
</script>
<div class="sub-header">
<div class="sub-header-status"><div class="sub-header-dot"></div><span data-i18n="sub_online">Online</span></div>
<div class="sub-header-version" id="subVersion">v1.0.0</div>
<a href="https://github.com/SubZ4242/turk-arr-bridge" target="_blank" style="font-size:.72rem;color:var(--text3);text-decoration:none;letter-spacing:.05em;font-family:'JetBrains Mono','Fira Code',monospace" onmouseover="this.style.color='var(--accent2)'" onmouseout="this.style.color='var(--text3)'">GitHub</a>
<div id="updateBanner">
<button class="ub-close" onclick="dismissUpdate()">&times;</button>
<div class="ub-title">🔄 Update verfügbar</div>
<div class="ub-msg" id="ubMsg">Neue Version auf GitHub verfügbar!</div>
<div class="ub-msg" id="ubDate" style="font-size:.68rem;color:#94a3b8;margin-top:2px"></div>
<div class="ub-actions">
<a class="ub-btn primary" href="https://github.com/SubZ4242/turk-arr-bridge" target="_blank">GitHub öffnen</a>
<button class="ub-btn dismiss" onclick="dismissUpdate()">Später</button>
</div>
</div>
</div>
</body>
</html>"""


# ── In-Memory Log-Ring ──
_log_buffer = []
_log_buffer_max = 500

class _GUILogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        _log_buffer.append(msg)
        if len(_log_buffer) > _log_buffer_max:
            _log_buffer.pop(0)

_gui_handler = _GUILogHandler()
_gui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_gui_handler)

_start_time = datetime.now()

# ── GitHub Update-Checker ──
import hashlib as _hl

_GITHUB_API_URL = "https://api.github.com/repos/SubZ4242/turk-arr-bridge/commits?path=bridge.py&per_page=1"
_GITHUB_CONTENT_API = "https://api.github.com/repos/SubZ4242/turk-arr-bridge/contents/bridge.py"
_update_info = {"update_available": False, "remote_sha": "", "remote_date": "", "message": "", "checked": ""}
_update_lock = threading.Lock()

def _get_local_bridge_hash() -> str:
    """SHA256 der aktuell laufenden bridge.py."""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
        with open(p, "rb") as f:
            return _hl.sha256(f.read()).hexdigest()
    except Exception:
        return ""

def _get_git_blob_sha(content: bytes) -> str:
    """Berechnet den Git Blob SHA1 für einen Dateiinhalt (identisch mit git hash-object)."""
    header = f"blob {len(content)}\0".encode()
    return _hl.sha1(header + content).hexdigest()

def _check_github_update():
    """Prüft ob auf GitHub eine neuere Version von bridge.py liegt."""
    global _update_info
    try:
        # Lokale Datei als Git-Blob-SHA berechnen (gleich wie git hash-object)
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
        with open(p, "rb") as f:
            local_content = f.read()
        local_blob_sha = _get_git_blob_sha(local_content)

        # GitHub Contents API: liefert den Git-Blob-SHA direkt (kein CDN-Caching Problem)
        headers = {"Accept": "application/vnd.github.v3+json"}
        content_resp = requests.get(_GITHUB_CONTENT_API, timeout=15, headers=headers)
        if not content_resp.ok:
            print(f"[UPDATE] GitHub API HTTP {content_resp.status_code}")
            return
        content_data = content_resp.json()
        remote_blob_sha = content_data.get("sha", "")

        if not remote_blob_sha:
            return

        if remote_blob_sha == local_blob_sha:
            with _update_lock:
                _update_info = {
                    "update_available": False, "remote_sha": remote_blob_sha[:12],
                    "remote_date": "", "message": "Aktuelle Version läuft bereits.",
                    "checked": datetime.now().strftime("%d.%m.%Y %H:%M")
                }
            return

        # Commit-Info holen (Datum, Nachricht)
        remote_date = ""
        commit_msg = ""
        try:
            api_resp = requests.get(_GITHUB_API_URL, timeout=10, headers=headers)
            if api_resp.ok:
                commits = api_resp.json()
                if commits:
                    remote_date = commits[0].get("commit", {}).get("committer", {}).get("date", "")[:10]
                    commit_msg = commits[0].get("commit", {}).get("message", "").split("\n")[0][:80]
        except Exception:
            pass

        with _update_lock:
            _update_info = {
                "update_available": True, "remote_sha": remote_blob_sha[:12],
                "remote_date": remote_date,
                "message": commit_msg or "Neue Version verfügbar auf GitHub!",
                "checked": datetime.now().strftime("%d.%m.%Y %H:%M")
            }
        print(f"[UPDATE] ⬆️ Neue Version auf GitHub verfügbar (Blob: {remote_blob_sha[:12]} ≠ {local_blob_sha[:12]})")
    except Exception as e:
        print(f"[UPDATE] Fehler beim GitHub-Check: {e}")

def _update_check_loop():
    """Hintergrund-Thread: Prüft alle 30 Min ob neue Version auf GitHub."""
    time.sleep(60)  # Beim Start 1 Min warten
    while True:
        _check_github_update()
        time.sleep(1800)  # Alle 30 Minuten

def _start_update_check_thread():
    t = threading.Thread(target=_update_check_loop, daemon=True)
    t.start()
    print("[UPDATE] GitHub Update-Check Thread gestartet")

# GitHub Update-Check Thread direkt starten (auch unter gunicorn)
_start_update_check_thread()

import os as _os
app.secret_key = _os.environ.get("TAB_SECRET_KEY", "turk-arr-bridge-secret-2024")
app.permanent_session_lifetime = timedelta(days=30)

LOGIN_HTML = r"""
<!DOCTYPE html><html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Türk ARR Bridge – Login</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🇹🇷</text></svg>">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #1e2235;
    --accent: #2563eb;
    --accent-hover: #1d4ed8;
    --text: #e2e8f0;
    --text-dim: #8892a4;
    --danger: #ef4444;
    --radius: 12px;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 20vh;
  }
  .login-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 40px 36px;
    width: 100%;
    max-width: 380px;
    box-shadow: 0 8px 40px rgba(0,0,0,.6);
  }
  .login-logo {
    text-align: center;
    font-size: 2.2rem;
    margin-bottom: 8px;
  }
  .login-title {
    text-align: center;
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 4px;
  }
  .login-sub {
    text-align: center;
    font-size: 0.78rem;
    color: var(--text-dim);
    margin-bottom: 28px;
  }
  label {
    display: block;
    font-size: 0.8rem;
    color: var(--text-dim);
    margin-bottom: 6px;
    margin-top: 16px;
  }
  input[type=text], input[type=password] {
    width: 100%;
    background: #0f1117;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.95rem;
    padding: 10px 14px;
    outline: none;
    transition: border-color .2s;
  }
  input[type=text]:focus, input[type=password]:focus {
    border-color: var(--accent);
  }
  .btn-login {
    width: 100%;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 600;
    padding: 11px;
    margin-top: 24px;
    cursor: pointer;
    transition: background .2s;
  }
  .btn-login:hover { background: var(--accent-hover); }
  .error-msg {
    background: rgba(239,68,68,.12);
    border: 1px solid rgba(239,68,68,.3);
    color: var(--danger);
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 0.85rem;
    margin-top: 18px;
    text-align: center;
  }
</style>
</head>
<body>
<div class="login-card">
  <div class="login-logo">🇹🇷</div>
  <div class="login-title">Türk ARR Bridge</div>
  <div class="login-sub">Bitte melde dich an</div>
  <form method="POST" action="/gui/login">
    <label for="username">Benutzername</label>
    <input type="text" id="username" name="username" autocomplete="username" autofocus required>
    <label for="password">Passwort</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required>
    {% if error %}
    <div class="error-msg">{{ error }}</div>
    {% endif %}
    <button type="submit" class="btn-login">Anmelden</button>
  </form>
</div>
</body></html>
"""


SETUP_HTML = r"""
<!DOCTYPE html><html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Türk ARR Bridge – Ersteinrichtung</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🇹🇷</text></svg>">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #1e2235;
    --accent: #7c3aed;
    --accent-hover: #6d28d9;
    --text: #e2e8f0;
    --text-dim: #8892a4;
    --danger: #ef4444;
    --success: #10b981;
    --radius: 12px;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 20vh;
  }
  .setup-card {
    background: var(--card);
    border: 1px solid #c8102e;
    border-radius: var(--radius);
    padding: 40px 36px;
    width: 100%;
    max-width: 400px;
    box-shadow: 0 8px 40px rgba(200,16,46,.25), 0 0 0 1px rgba(200,16,46,.15);
  }
  .setup-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(124,58,237,.15);
    border: 1px solid rgba(124,58,237,.3);
    color: #a78bfa;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: .06em;
    text-transform: uppercase;
    padding: 4px 10px;
    border-radius: 20px;
    margin-bottom: 14px;
  }
  .setup-logo { text-align: center; font-size: 2.2rem; margin-bottom: 8px; }
  .setup-title { text-align: center; font-size: 1.1rem; font-weight: 700; color: var(--text); margin-bottom: 4px; }
  .setup-sub { text-align: center; font-size: 0.78rem; color: var(--text-dim); margin-bottom: 24px; }
  .setup-hint {
    background: rgba(124,58,237,.08);
    border: 1px solid rgba(124,58,237,.2);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 0.8rem;
    color: #a78bfa;
    margin-bottom: 20px;
    line-height: 1.5;
  }
  label { display: block; font-size: 0.8rem; color: var(--text-dim); margin-bottom: 6px; margin-top: 16px; }
  input[type=text], input[type=password] {
    width: 100%;
    background: #0f1117;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.95rem;
    padding: 10px 14px;
    outline: none;
    transition: border-color .2s;
  }
  input[type=text]:focus, input[type=password]:focus { border-color: var(--accent); }
  .btn-setup {
    width: 100%;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 600;
    padding: 11px;
    margin-top: 24px;
    cursor: pointer;
    transition: background .2s;
  }
  .btn-setup:hover { background: var(--accent-hover); }
  .error-msg {
    background: rgba(239,68,68,.12);
    border: 1px solid rgba(239,68,68,.3);
    color: var(--danger);
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 0.85rem;
    margin-top: 18px;
    text-align: center;
  }
  .divider { text-align: center; display: flex; align-items: center; gap: 10px; margin-top: 20px; }
  .divider-line { flex: 1; height: 1px; background: var(--border); }
  .divider-text { font-size: 0.72rem; color: var(--text-dim); }
</style>
</head>
<body>
<div class="setup-card">
  <div style="display:flex;justify-content:center">
    <div class="setup-badge">✦ Ersteinrichtung</div>
  </div>
  <div class="setup-logo">🇹🇷</div>
  <div class="setup-title">Türk ARR Bridge</div>
  <div class="setup-sub">Willkommen! Lege deinen Admin-Zugang an.</div>
  <div class="setup-hint">
    🔐 Dieser Schritt erscheint nur einmal.<br>
    Nach dem Speichern ist die GUI mit diesen Zugangsdaten geschützt.
  </div>
  <form method="POST" action="/gui/setup">
    <label for="username">Benutzername</label>
    <input type="text" id="username" name="username" autocomplete="username" autofocus required placeholder="z. B. admin">
    <label for="password">Passwort <span style="color:var(--text-dim);font-size:.72rem">(min. 6 Zeichen)</span></label>
    <input type="password" id="password" name="password" autocomplete="new-password" required>
    <label for="password2">Passwort bestätigen</label>
    <input type="password" id="password2" name="password2" autocomplete="new-password" required>
    {% if error %}
    <div class="error-msg">{{ error }}</div>
    {% endif %}
    <button type="submit" class="btn-setup">🚀 Zugang anlegen &amp; einloggen</button>
  </form>
</div>
</body></html>
"""


def _check_gui_auth():
    """before_request-Hook: prüft Session-Auth für alle /gui/-Routen."""
    if not request.path.startswith("/gui"):
        return None
    # Setup/Login/Logout-Routen immer erreichbar
    if request.path in ("/gui/login", "/gui/logout", "/gui/setup"):
        return None
    cfg = _load_config()
    user = cfg.get("gui_user", "").strip()
    pw   = cfg.get("gui_pass", "").strip()
    if not (user and pw):
        # Noch kein Benutzer angelegt → zur Ersteinrichtung
        return redirect("/gui/setup")
    if not session.get("gui_authenticated"):
        return redirect("/gui/login")
    return None


app.before_request(_check_gui_auth)


@app.route("/gui/login", methods=["GET", "POST"])
def gui_login():
    cfg = _load_config()
    user = cfg.get("gui_user", "").strip()
    pw   = cfg.get("gui_pass", "").strip()
    if not (user and pw):
        return redirect("/gui/setup")
    if request.method == "POST":
        req_user = request.form.get("username", "").strip()
        req_pw   = request.form.get("password", "").strip()
        if req_user == user and req_pw == pw:
            session.permanent = True
            session["gui_authenticated"] = True
            return redirect("/gui/")
        return render_template_string(LOGIN_HTML, error="Falscher Benutzername oder Passwort.")
    return render_template_string(LOGIN_HTML, error="")


@app.route("/gui/setup", methods=["GET", "POST"])
def gui_setup():
    cfg = _load_config()
    # Setup nur erlaubt wenn noch kein User angelegt
    if cfg.get("gui_user", "").strip() and cfg.get("gui_pass", "").strip():
        return redirect("/gui/login")
    if request.method == "POST":
        new_user = request.form.get("username", "").strip()
        new_pw   = request.form.get("password", "").strip()
        new_pw2  = request.form.get("password2", "").strip()
        if not new_user:
            return render_template_string(SETUP_HTML, error="Benutzername darf nicht leer sein.")
        if len(new_pw) < 6:
            return render_template_string(SETUP_HTML, error="Passwort muss mindestens 6 Zeichen haben.")
        if new_pw != new_pw2:
            return render_template_string(SETUP_HTML, error="Passwörter stimmen nicht überein.")
        cfg["gui_user"] = new_user
        cfg["gui_pass"] = new_pw
        _save_config(cfg)
        global _config
        _config = cfg
        session.permanent = True
        session["gui_authenticated"] = True
        return redirect("/gui/")
    return render_template_string(SETUP_HTML, error="")


@app.route("/gui/logout")
def gui_logout():
    session.clear()
    return redirect("/gui/login")


@app.route("/gui")
@app.route("/gui/")
def gui_page():
    return render_template_string(GUI_HTML)


@app.route("/gui/music.mp3")
def gui_music():
    """Liefert die Header-Musik als MP3 aus."""
    base = os.path.dirname(os.path.abspath(__file__))
    # Zuerst im static/-Unterordner suchen (GitHub-Struktur)
    for candidate in [
        os.path.join(base, "static", "header_music.mp3"),
        os.path.join(base, "header_music.mp3"),
        "/config/header_music.mp3",
    ]:
        if os.path.exists(candidate):
            return send_file(candidate, mimetype="audio/mpeg")
    return "", 404


@app.route("/gui/api/health")
def gui_health():
    stats = title_cache.stats()
    uptime_sec = (datetime.now() - _start_time).total_seconds()
    if uptime_sec < 3600:
        uptime_str = f"{int(uptime_sec//60)}m"
    elif uptime_sec < 86400:
        uptime_str = f"{int(uptime_sec//3600)}h {int((uptime_sec%3600)//60)}m"
    else:
        uptime_str = f"{int(uptime_sec//86400)}d {int((uptime_sec%86400)//3600)}h"
    # Zähle Serien/Filme
    sonarr_count = radarr_count = "?"
    try:
        r = requests.get(f"{SONARR_URL}/api/v3/series", params={"apikey": SONARR_API_KEY}, timeout=5)
        if r.ok: sonarr_count = len(r.json())
    except: pass
    try:
        r = requests.get(f"{RADARR_URL}/api/v3/movie", params={"apikey": RADARR_API_KEY}, timeout=5)
        if r.ok: radarr_count = len(r.json())
    except: pass
    return jsonify({
        "status": "ok", "uptime": uptime_str,
        "sonarr_series": sonarr_count, "radarr_movies": radarr_count,
        "title_cache": stats,
        "learned_count": len(_learned_db),
    })


@app.route("/gui/api/update-check")
def gui_update_check():
    with _update_lock:
        return jsonify(_update_info)


@app.route("/gui/api/mappings-count")
def gui_mappings_count():
    return jsonify({"count": len(title_cache._cache)})


@app.route("/gui/api/mappings")
def gui_mappings():
    refresh_title_cache(title_cache)
    mappings = {}
    for key, entry in title_cache._cache.items():
        mappings[key] = sorted(entry["titles"])
    return jsonify({"count": len(mappings), "mappings": mappings})


@app.route("/gui/api/learned")
def gui_learned():
    """Gibt alle gelernten Mappings aus der persistenten Lern-DB zurück."""
    with _learned_db_lock:
        snapshot = dict(_learned_db)
    items = []
    for key, entry in sorted(snapshot.items(), key=lambda x: x[1].get("search_count", 0), reverse=True):
        items.append({
            "key": key,
            "titles": entry.get("titles", []),
            "source": entry.get("source", "?"),
            "learned_at": entry.get("learned_at", ""),
            "updated_at": entry.get("updated_at", ""),
            "search_count": entry.get("search_count", 0),
        })
    return jsonify({"count": len(items), "learned": items})


@app.route("/gui/api/learned/<path:key>", methods=["DELETE"])
def gui_learned_delete(key):
    """Löscht einen einzelnen gelernten Eintrag."""
    with _learned_db_lock:
        if key in _learned_db:
            del _learned_db[key]
            _save_learned_db()
            # Auch aus In-Memory-Cache entfernen
            title_cache._cache.pop(key, None)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Nicht gefunden"}), 404


@app.route("/gui/api/learned", methods=["DELETE"])
def gui_learned_clear():
    """Löscht ALLE gelernten Mappings."""
    global _learned_db
    with _learned_db_lock:
        _learned_db = {}
    _save_learned_db()
    return jsonify({"ok": True})


@app.route("/gui/api/test-connections")
def gui_test_connections():
    results = []
    # Sonarr
    try:
        r = requests.get(f"{SONARR_URL}/api/v3/system/status", params={"apikey": SONARR_API_KEY}, timeout=8)
        if r.ok:
            d = r.json()
            results.append({"name": "Sonarr (TR)", "url": SONARR_URL, "status": "ok",
                            "detail": f"v{d.get('version','')} – {d.get('osName','')}"})
        else:
            results.append({"name": "Sonarr (TR)", "url": SONARR_URL, "status": "err", "detail": f"HTTP {r.status_code}"})
    except Exception as e:
        results.append({"name": "Sonarr (TR)", "url": SONARR_URL, "status": "err", "detail": str(e)[:80]})
    # Radarr
    try:
        r = requests.get(f"{RADARR_URL}/api/v3/system/status", params={"apikey": RADARR_API_KEY}, timeout=8)
        if r.ok:
            d = r.json()
            results.append({"name": "Radarr (TR)", "url": RADARR_URL, "status": "ok",
                            "detail": f"v{d.get('version','')} – {d.get('osName','')}"})
        else:
            results.append({"name": "Radarr (TR)", "url": RADARR_URL, "status": "err", "detail": f"HTTP {r.status_code}"})
    except Exception as e:
        results.append({"name": "Radarr (TR)", "url": RADARR_URL, "status": "err", "detail": str(e)[:80]})
    # Jackett
    try:
        r = requests.get(f"{JACKETT_URL}/api/v2.0/server/config", timeout=8)
        if r.ok:
            results.append({"name": "Jackett", "url": JACKETT_URL, "status": "ok", "detail": "Erreichbar"})
        else:
            results.append({"name": "Jackett", "url": JACKETT_URL, "status": "err", "detail": f"HTTP {r.status_code}"})
    except Exception as e:
        results.append({"name": "Jackett", "url": JACKETT_URL, "status": "err", "detail": str(e)[:80]})
    # Upstream Torznab (TürkTorrent)
    try:
        r = requests.get(UPSTREAM_TORZNAB_URL, params={"apikey": JACKETT_API_KEY, "t": "caps"}, timeout=10)
        if r.ok:
            results.append({"name": "TürkTorrent (Torznab)", "url": UPSTREAM_TORZNAB_URL[:60], "status": "ok", "detail": "Torznab-Caps OK"})
        else:
            results.append({"name": "TürkTorrent (Torznab)", "url": UPSTREAM_TORZNAB_URL[:60], "status": "err", "detail": f"HTTP {r.status_code}"})
    except Exception as e:
        results.append({"name": "TürkTorrent (Torznab)", "url": UPSTREAM_TORZNAB_URL[:60], "status": "err", "detail": str(e)[:80]})
    # qBittorrent
    try:
        sess, ver, error = qbit_connect(force_login=True)
        if sess is not None:
            results.append({"name": "qBittorrent", "url": QBIT_URL, "status": "ok", "detail": f"Version {ver}"})
        else:
            results.append({"name": "qBittorrent", "url": QBIT_URL, "status": "err", "detail": error})
    except Exception as e:
        results.append({"name": "qBittorrent", "url": QBIT_URL, "status": "err", "detail": str(e)[:80]})
    # Telegram
    if TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=8)
            if r.ok:
                bot = r.json().get("result", {})
                results.append({"name": "Telegram Bot", "url": f"@{bot.get('username','?')}", "status": "ok", "detail": bot.get("first_name", "OK")})
            else:
                results.append({"name": "Telegram Bot", "url": "—", "status": "err", "detail": f"HTTP {r.status_code}"})
        except Exception as e:
            results.append({"name": "Telegram Bot", "url": "—", "status": "err", "detail": str(e)[:80]})
    return jsonify({"results": results})


@app.route("/gui/api/test-single", methods=["POST"])
def gui_test_single():
    data = request.json or {}
    service = data.get("service", "")
    if service == "sonarr":
        url = data.get("sonarr_url", SONARR_URL)
        key = data.get("sonarr_api_key", SONARR_API_KEY)
        try:
            r = requests.get(f"{url}/api/v3/system/status", params={"apikey": key}, timeout=8)
            if r.ok:
                d = r.json()
                return jsonify({"status": "ok", "detail": f"v{d.get('version','')} – {len(requests.get(f'{url}/api/v3/series', params={'apikey':key}, timeout=5).json())} Serien"})
            return jsonify({"status": "err", "detail": f"HTTP {r.status_code}"})
        except Exception as e:
            return jsonify({"status": "err", "detail": str(e)[:100]})
    elif service == "radarr":
        url = data.get("radarr_url", RADARR_URL)
        key = data.get("radarr_api_key", RADARR_API_KEY)
        try:
            r = requests.get(f"{url}/api/v3/system/status", params={"apikey": key}, timeout=8)
            if r.ok:
                d = r.json()
                return jsonify({"status": "ok", "detail": f"v{d.get('version','')} – {len(requests.get(f'{url}/api/v3/movie', params={'apikey':key}, timeout=5).json())} Filme"})
            return jsonify({"status": "err", "detail": f"HTTP {r.status_code}"})
        except Exception as e:
            return jsonify({"status": "err", "detail": str(e)[:100]})
    elif service == "jackett":
        url = data.get("jackett_url", JACKETT_URL)
        key = data.get("jackett_api_key", JACKETT_API_KEY)
        upstream = data.get("upstream_torznab_url", UPSTREAM_TORZNAB_URL)
        try:
            r = requests.get(f"{url}/api/v2.0/server/config", timeout=8)
            if not r.ok:
                return jsonify({"status": "err", "detail": f"Jackett HTTP {r.status_code}"})
            r2 = requests.get(upstream, params={"apikey": key, "t": "caps"}, timeout=10)
            if r2.ok:
                return jsonify({"status": "ok", "detail": "Jackett + TürkTorrent Torznab OK"})
            return jsonify({"status": "err", "detail": f"Torznab HTTP {r2.status_code}"})
        except Exception as e:
            return jsonify({"status": "err", "detail": str(e)[:100]})
    elif service == "qbit":
        url = data.get("qbit_url", QBIT_URL)
        user = data.get("qbit_user", QBIT_USER)
        pw = data.get("qbit_pass", QBIT_PASS)
        try:
            sess, ver, error = qbit_connect(
                qbit_url=url,
                username=user,
                password=pw,
                force_login=True,
            )
            if sess is not None:
                return jsonify({"status": "ok", "detail": f"qBittorrent {ver} verbunden"})
            return jsonify({"status": "err", "detail": error})
        except Exception as e:
            return jsonify({"status": "err", "detail": str(e)[:100]})
    return jsonify({"status": "err", "detail": "Unbekannter Service"})


@app.route("/gui/api/test-telegram", methods=["POST"])
def gui_test_telegram():
    """Sendet eine Test-Nachricht über Telegram."""
    data = request.json or {}
    token = data.get("token", TELEGRAM_BOT_TOKEN)
    chat_id = data.get("chat_id", TELEGRAM_CHAT_ID)
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Bot Token und Chat ID erforderlich"})
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\u2705 Turk ARR Bridge – Test-Nachricht erfolgreich!", "parse_mode": "HTML"},
            timeout=10,
        )
        if r.ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": r.json().get("description", f"HTTP {r.status_code}")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:100]})


@app.route("/gui/api/config", methods=["GET"])
def gui_get_config():
    # gui_pass nicht zurückgeben – wird im Frontend nie angezeigt
    # verhindert, dass saveConfig() das Passwort mit leerem Wert überschreibt
    safe = {k: v for k, v in _config.items() if k != "gui_pass"}
    return jsonify(safe)


@app.route("/gui/api/config", methods=["POST"])
def gui_save_config():
    global _config, SONARR_URL, SONARR_API_KEY, RADARR_URL, RADARR_API_KEY
    global JACKETT_URL, JACKETT_API_KEY, UPSTREAM_TORZNAB_URL, CACHE_TTL_SECONDS, LOG_LEVEL
    global QBIT_URL, QBIT_USER, QBIT_PASS
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
    global BOXSET_AUTO_DOWNLOAD, BOXSET_PREFER_SEEDERS
    global TURKTORRENT_USERNAME, TURKTORRENT_PASSWORD, TURKTORRENT_COOKIE_AUTO_REFRESH
    global TURKTORRENT_COOKIE_INTERVAL, TURKTORRENT_SITE_URL, TURKTORRENT_JACKETT_INDEXER_ID
    global FLARESOLVERR_URL
    data = request.json or {}
    for k in _DEFAULT_CONFIG:
        if k in data and data[k] is not None:
            val = data[k]
            # gui_pass nie mit leerem Wert überschreiben (Passwortfeld wird leer gesendet)
            if k == "gui_pass" and str(val).strip() == "":
                continue
            if k in (
                "bridge_port", "cache_ttl_seconds",
                "arr_indexer_heal_interval_minutes",
            ):
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    pass
            _config[k] = val
    _save_config(_config)
    # Globale Variablen aktualisieren
    SONARR_URL = _config["sonarr_url"]
    SONARR_API_KEY = _config["sonarr_api_key"]
    RADARR_URL = _config["radarr_url"]
    RADARR_API_KEY = _config["radarr_api_key"]
    JACKETT_URL = _config["jackett_url"]
    JACKETT_API_KEY = _config["jackett_api_key"]
    UPSTREAM_TORZNAB_URL = _config["upstream_torznab_url"]
    CACHE_TTL_SECONDS = int(_config["cache_ttl_seconds"])
    LOG_LEVEL = _config["log_level"]
    QBIT_URL = _config["qbit_url"]
    QBIT_USER = _config["qbit_user"]
    QBIT_PASS = _config["qbit_pass"]
    TELEGRAM_BOT_TOKEN = _config["telegram_bot_token"]
    TELEGRAM_CHAT_ID = str(_config["telegram_chat_id"])
    TELEGRAM_ENABLED = _config.get("telegram_enabled", True)
    BOXSET_AUTO_DOWNLOAD = _config.get("boxset_auto_download", True)
    BOXSET_PREFER_SEEDERS = _config.get("boxset_prefer_seeders", False)
    TURKTORRENT_USERNAME = _config.get("turktorrent_username", "")
    TURKTORRENT_PASSWORD = _config.get("turktorrent_password", "")
    TURKTORRENT_COOKIE_AUTO_REFRESH = _config.get("turktorrent_cookie_auto_refresh", True)
    TURKTORRENT_COOKIE_INTERVAL = int(_config.get("turktorrent_cookie_interval_minutes", 120))
    TURKTORRENT_SITE_URL = _config.get("turktorrent_site_url", "https://turktorrent.us")
    TURKTORRENT_JACKETT_INDEXER_ID = _config.get("turktorrent_jackett_indexer_id", "turktorrent")
    FLARESOLVERR_URL = _config.get("flaresolverr_url", "")
    title_cache.ttl = CACHE_TTL_SECONDS
    title_cache._last_refresh = None  # Force refresh
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    logger.info(f"Konfiguration über GUI aktualisiert")
    _request_arr_indexer_heal()
    return jsonify({"ok": True})


@app.route("/gui/api/indexer-status")
def gui_indexer_status():
    """Gibt den aktuellen Status des TurkTorrent Cookie-Refreshs zurück."""
    cookie_status = _config.get("turktorrent_cookie_status", "Noch kein Refresh durchgeführt")
    last_refresh = _config.get("turktorrent_last_cookie_refresh", "")
    auto_refresh = _config.get("turktorrent_cookie_auto_refresh", True)
    interval = int(_config.get("turktorrent_cookie_interval_minutes", 120))

    next_refresh = ""
    if last_refresh and auto_refresh:
        try:
            last_dt = datetime.fromisoformat(last_refresh)
            next_dt = last_dt + timedelta(minutes=interval)
            next_refresh = next_dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass

    last_refresh_fmt = ""
    if last_refresh:
        try:
            last_refresh_fmt = datetime.fromisoformat(last_refresh).strftime("%d.%m.%Y %H:%M")
        except Exception:
            last_refresh_fmt = last_refresh

    flaresolverr_url = _config.get("flaresolverr_url", "")
    flaresolverr_ok = False
    if flaresolverr_url:
        try:
            fr = requests.get(f"{flaresolverr_url.rstrip('/')}/health", timeout=5)
            flaresolverr_ok = fr.ok
        except Exception:
            pass

    return jsonify({
        "cookie_status": cookie_status,
        "last_refresh": last_refresh_fmt,
        "auto_refresh_enabled": auto_refresh,
        "next_refresh": next_refresh,
        "interval_minutes": interval,
        "flaresolverr_ok": flaresolverr_ok,
        "flaresolverr_url": flaresolverr_url,
    })


@app.route("/gui/api/refresh-cookie", methods=["POST"])
def gui_refresh_cookie():
    """Manueller Cookie-Refresh."""
    with _cookie_refresh_lock:
        result = _do_cookie_refresh()
    return jsonify(result)


@app.route("/gui/api/test-turktorrent-login", methods=["POST"])
def gui_test_turktorrent_login():
    """Testet den TurkTorrent Login via FlareSolverr (Captcha per Telegram)."""
    data = request.json or {}
    username = data.get("username", _config.get("turktorrent_username", ""))
    password = data.get("password", _config.get("turktorrent_password", ""))
    site_url = data.get("site_url", _config.get("turktorrent_site_url", "https://turktorrent.us"))
    flaresolverr_url = data.get("flaresolverr_url", _config.get("flaresolverr_url", ""))

    if not username or not password:
        return jsonify({"ok": False, "error": "Username und Passwort erforderlich"})
    if not flaresolverr_url:
        return jsonify({"ok": False, "error": "FlareSolverr URL erforderlich"})

    result = _turktorrent_login(username, password, site_url, flaresolverr_url)
    return jsonify(result)


@app.route("/gui/api/test-flaresolverr", methods=["POST"])
def gui_test_flaresolverr():
    """Testet ob FlareSolverr erreichbar ist."""
    data = request.json or {}
    flaresolverr_url = data.get("flaresolverr_url", _config.get("flaresolverr_url", ""))

    if not flaresolverr_url:
        return jsonify({"ok": False, "error": "FlareSolverr URL nicht angegeben"})

    try:
        resp = requests.get(f"{flaresolverr_url.rstrip('/')}/health", timeout=10)
        if resp.ok:
            data = resp.json()
            return jsonify({"ok": True, "version": data.get("version", "unbekannt")})
        return jsonify({"ok": False, "error": f"HTTP {resp.status_code}"})
    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": f"Nicht erreichbar unter {flaresolverr_url} – läuft der Container?"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:100]})


@app.route("/gui/api/change-password", methods=["POST"])
def gui_change_password():
    global _config
    data = request.json or {}
    old_pass = data.get("old_pass", "")
    new_pass = data.get("new_pass", "").strip()
    current_pw = _config.get("gui_pass", "").strip()
    if not old_pass or old_pass.strip() != current_pw:
        return jsonify({"ok": False, "error": "wrong_password"}), 403
    if not new_pass:
        return jsonify({"ok": False, "error": "empty"}), 400
    _config["gui_pass"] = new_pass
    _save_config(_config)
    logger.info("GUI Passwort über Settings geändert")
    return jsonify({"ok": True})


@app.route("/gui/api/search")
def gui_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q parameter required"}), 400
    refresh_title_cache(title_cache)
    titles = title_cache.get_search_titles(q)
    seen_normalized = set()
    unique_titles = []
    for title in titles:
        norm = normalize_for_search(title)
        if norm and norm not in seen_normalized:
            seen_normalized.add(norm)
            unique_titles.append(title)
    results_by_source = {}
    total_results = 0
    for search_title in unique_titles:
        try:
            resp = requests.get(UPSTREAM_TORZNAB_URL,
                params={"apikey": JACKETT_API_KEY, "q": search_title, "t": "search"}, timeout=30)
            if resp.ok:
                tree = etree.fromstring(resp.content)
                items = tree.findall(".//item")
                result_titles = [{"title": it.find("title").text} for it in items if it.find("title") is not None and it.find("title").text]
                results_by_source[search_title] = result_titles
                total_results += len(result_titles)
        except Exception as e:
            results_by_source[search_title] = [{"error": str(e)}]
    return jsonify({
        "query": q, "search_variants": unique_titles,
        "total_results": total_results, "results_by_source": results_by_source,
    })


@app.route("/gui/api/backup", methods=["GET"])
def gui_backup():
    """Konfiguration als JSON-Datei herunterladen."""
    import io
    cfg_copy = dict(_config)
    data = json.dumps(cfg_copy, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=bridge_backup.json"}
    )


@app.route("/gui/api/restore", methods=["POST"])
def gui_restore():
    """Konfiguration aus hochgeladener JSON-Datei wiederherstellen."""
    global _config, SONARR_URL, SONARR_API_KEY, RADARR_URL, RADARR_API_KEY
    global JACKETT_URL, JACKETT_API_KEY, UPSTREAM_TORZNAB_URL, CACHE_TTL_SECONDS, LOG_LEVEL
    global QBIT_URL, QBIT_USER, QBIT_PASS
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
    global BOXSET_AUTO_DOWNLOAD, BOXSET_PREFER_SEEDERS
    global TURKTORRENT_USERNAME, TURKTORRENT_PASSWORD, TURKTORRENT_COOKIE_AUTO_REFRESH
    global TURKTORRENT_COOKIE_INTERVAL, TURKTORRENT_SITE_URL, TURKTORRENT_JACKETT_INDEXER_ID
    try:
        uploaded = request.json or {}
        if not uploaded:
            return jsonify({"ok": False, "error": "Keine Daten empfangen"}), 400
        for k in _DEFAULT_CONFIG:
            if k in uploaded and uploaded[k] is not None:
                val = uploaded[k]
                if k in (
                    "bridge_port", "cache_ttl_seconds",
                    "arr_indexer_heal_interval_minutes",
                ):
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        pass
                _config[k] = val
        _save_config(_config)
        SONARR_URL = _config["sonarr_url"]
        SONARR_API_KEY = _config["sonarr_api_key"]
        RADARR_URL = _config["radarr_url"]
        RADARR_API_KEY = _config["radarr_api_key"]
        JACKETT_URL = _config["jackett_url"]
        JACKETT_API_KEY = _config["jackett_api_key"]
        UPSTREAM_TORZNAB_URL = _config["upstream_torznab_url"]
        CACHE_TTL_SECONDS = int(_config["cache_ttl_seconds"])
        LOG_LEVEL = _config["log_level"]
        QBIT_URL = _config["qbit_url"]
        QBIT_USER = _config["qbit_user"]
        QBIT_PASS = _config["qbit_pass"]
        TELEGRAM_BOT_TOKEN = _config["telegram_bot_token"]
        TELEGRAM_CHAT_ID = str(_config["telegram_chat_id"])
        TELEGRAM_ENABLED = _config.get("telegram_enabled", True)
        BOXSET_AUTO_DOWNLOAD = _config.get("boxset_auto_download", True)
        BOXSET_PREFER_SEEDERS = _config.get("boxset_prefer_seeders", False)
        TURKTORRENT_USERNAME = _config.get("turktorrent_username", "")
        TURKTORRENT_PASSWORD = _config.get("turktorrent_password", "")
        TURKTORRENT_COOKIE_AUTO_REFRESH = _config.get("turktorrent_cookie_auto_refresh", True)
        TURKTORRENT_COOKIE_INTERVAL = int(_config.get("turktorrent_cookie_interval_minutes", 120))
        TURKTORRENT_SITE_URL = _config.get("turktorrent_site_url", "https://turktorrent.us")
        TURKTORRENT_JACKETT_INDEXER_ID = _config.get("turktorrent_jackett_indexer_id", "turktorrent")
        FLARESOLVERR_URL = _config.get("flaresolverr_url", "")
        TWOCAPTCHA_API_KEY = _config.get("twocaptcha_api_key", "")
        title_cache.ttl = CACHE_TTL_SECONDS
        title_cache._last_refresh = None
        logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
        logger.info("Konfiguration über Restore wiederhergestellt")
        _request_arr_indexer_heal()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/gui/api/refresh-cache", methods=["POST"])
def gui_refresh_cache():
    title_cache._last_refresh = None
    refresh_title_cache(title_cache)
    return jsonify({"ok": True, "entries": len(title_cache._cache)})


@app.route("/gui/api/logs")
def gui_logs():
    return jsonify({"logs": "\n".join(_log_buffer[-200:])})


# ============================================================
# hCaptcha – Manuelle Lösung per Telegram-Link
# ============================================================

@app.route("/captcha")
def captcha_page():
    """Handy-freundliche, Tailscale-/Reverse-Proxy-feste Captcha-Seite."""
    sitekey = _pending_captcha_sitekey or _DEFAULT_TURKTORRENT_HCAPTCHA_SITEKEY
    captcha_host = _pending_captcha_host or "turktorrent.us"
    active = _captcha_request_active
    captcha_api_url = "https://hcaptcha.com/1/api.js?" + urllib.parse.urlencode({
        "hl": "tr",
        # Die Originalseite lädt dasselbe Sitekey ausdrücklich für diesen Host.
        # Ohne den Host-Hinweis kann ein Widget auf Tailscale-/IP-URLs leer bleiben.
        "host": captcha_host,
        "render": "explicit",
        "onload": "renderCaptcha",
    })
    html = render_template_string("""<!DOCTYPE html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔐 TurkTorrent Captcha</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1923;color:#e8eaed;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.card{background:#1a2332;border-radius:16px;padding:28px;max-width:400px;width:100%;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.4);position:relative}
h1{font-size:1.3rem;margin-bottom:6px}
.sub{color:#8899aa;font-size:.85rem;margin-bottom:20px}
.h-captcha{display:flex;justify-content:center;min-height:78px;margin:16px 0}
#status{margin-top:16px;padding:12px;border-radius:10px;font-size:.9rem;display:none}
.ok{background:#1a3a2a;color:#4ade80;display:block!important}
.err{background:#3a1a1a;color:#f87171;display:block!important}
.waiting{background:#2a2a1a;color:#fbbf24;display:block!important}
.inactive{background:#1a2332;color:#8899aa;display:block!important;border:1px dashed #334}
.refresh-btn{position:absolute;top:16px;right:16px;background:none;border:none;color:#8899aa;cursor:pointer;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;transition:all .25s ease}
.refresh-btn:hover{color:#e8eaed;background:rgba(255,255,255,.08)}
.refresh-btn svg{width:20px;height:20px;transition:transform .4s ease}
.refresh-btn.spinning svg{animation:spin 1s linear infinite}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
.refresh-btn[disabled]{opacity:.4;cursor:not-allowed}
</style></head><body>
<div class="card">
<button class="refresh-btn" id="refreshBtn" onclick="requestNewCaptcha()" title="Neuen Cookie-Refresh anfordern">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21.5 2v6h-6"/><path d="M2.5 22v-6h6"/>
    <path d="M2.5 11.5a10 10 0 0 1 16.5-5.7L21.5 8"/>
    <path d="M21.5 12.5a10 10 0 0 1-16.5 5.7L2.5 16"/>
  </svg>
</button>
<h1>🔐 TurkTorrent hCaptcha</h1>
<p class="sub">Captcha lösen → Token wird automatisch an die Bridge gesendet</p>
{% if not active %}
<div id="status" class="inactive">⏸️ Kein Captcha angefordert.<br>Die Bridge wartet gerade nicht auf eine Lösung.</div>
{% else %}
<div class="h-captcha" id="captchaMount"></div>
<div id="status" class="waiting">⏳ Captcha wird geladen…</div>
{% endif %}
</div>
<script>
let captchaRendered = false;

function showCaptchaError(message) {
  const st = document.getElementById('status');
  if (!st) return;
  st.className = 'err';
  st.innerHTML = '❌ ' + message + '<br><small>Bitte im externen Browser öffnen und Inhaltsblocker für hcaptcha.com deaktivieren.</small>';
}

function renderCaptcha() {
  {% if active %}
  const mount = document.getElementById('captchaMount');
  if (!mount || !window.hcaptcha || captchaRendered) return;
  try {
    window.hcaptcha.render(mount, {
      sitekey: '{{ sitekey }}',
      theme: 'dark',
      size: 'compact',
      callback: onCaptchaSolved,
      'error-callback': function(code) { showCaptchaError('hCaptcha-Fehler: ' + code); },
      'expired-callback': function() { showCaptchaError('Captcha abgelaufen – Seite bitte neu laden.'); },
      'chalexpired-callback': function() { showCaptchaError('Aufgabe abgelaufen – Seite bitte neu laden.'); }
    });
    captchaRendered = true;
    const st = document.getElementById('status');
    st.className = '';
    st.textContent = '';
  } catch (e) {
    showCaptchaError('Widget konnte nicht angezeigt werden: ' + e.message);
  }
  {% endif %}
}

function onCaptchaSolved(token) {
  const st = document.getElementById('status');
  st.className = 'waiting';
  st.textContent = '⏳ Token wird gesendet...';
  fetch('/captcha-callback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    cache: 'no-store',
    body: JSON.stringify({token: token})
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      st.className = 'ok';
      st.innerHTML = '✅ Captcha gelöst!<br>Du kannst diese Seite schließen.';
    } else {
      st.className = 'err';
      st.textContent = '❌ ' + (d.error || 'Fehler');
    }
  })
  .catch(e => {
    st.className = 'err';
    st.textContent = '❌ Verbindungsfehler: ' + e.message;
  });
}

function requestNewCaptcha() {
  const btn = document.getElementById('refreshBtn');
  const st = document.getElementById('status');
  btn.classList.add('spinning');
  btn.disabled = true;
  st.className = 'waiting';
  st.textContent = '⏳ Cookie-Refresh wird gestartet… Captcha erscheint gleich.';

  fetch('/captcha-request-new', {method: 'POST', cache: 'no-store'})
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      // Poll until captcha is active, then reload
      pollForCaptcha();
    } else {
      btn.classList.remove('spinning');
      btn.disabled = false;
      st.className = 'err';
      st.textContent = '❌ ' + (d.error || 'Fehler beim Starten');
    }
  })
  .catch(e => {
    btn.classList.remove('spinning');
    btn.disabled = false;
    st.className = 'err';
    st.textContent = '❌ Verbindungsfehler: ' + e.message;
  });
}

function pollForCaptcha() {
  let attempts = 0;
  const maxAttempts = 60; // max 60s
  const iv = setInterval(() => {
    attempts++;
    fetch('/captcha-status?ts=' + Date.now(), {cache: 'no-store'})
    .then(r => r.json())
    .then(d => {
      if (d.active) {
        clearInterval(iv);
        const url = new URL(window.location.href);
        url.searchParams.set('request', Date.now().toString());
        window.location.replace(url.toString());
      } else if (attempts >= maxAttempts) {
        clearInterval(iv);
        const st = document.getElementById('status');
        const btn = document.getElementById('refreshBtn');
        btn.classList.remove('spinning');
        btn.disabled = false;
        st.className = 'err';
        st.textContent = '❌ Timeout – Captcha wurde nicht angefordert.';
      }
    })
    .catch(() => {});
  }, 1000);
}

{% if active %}
setTimeout(function() {
  if (!document.querySelector('#captchaMount iframe')) {
    showCaptchaError('hCaptcha wurde nicht geladen.');
  }
}, 12000);
{% endif %}
</script>
{% if active %}<script src="{{ captcha_api_url }}" async defer onerror="showCaptchaError('hCaptcha-Netzwerkdatei blockiert.')"></script>{% endif %}
</body></html>""", sitekey=sitekey, captcha_api_url=captcha_api_url, active=active)
    response = Response(html, content_type="text/html; charset=UTF-8")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/captcha-callback", methods=["POST"])
def captcha_callback():
    """Empfängt den hCaptcha-Token vom User (nach manueller Lösung)."""
    global _pending_captcha_token
    data = request.json or {}
    token = data.get("token", "").strip()

    if not token:
        return jsonify({"ok": False, "error": "Kein Token erhalten"})

    if not _captcha_request_active:
        return jsonify({"ok": False, "error": "Kein Captcha angefordert – die Bridge wartet gerade nicht"})

    _pending_captcha_token = token
    _pending_captcha_event.set()
    print(f"[CAPTCHA] ✅ Manueller Token empfangen: {token[:30]}...")
    return jsonify({"ok": True, "message": "Token empfangen – Login wird durchgeführt"})


@app.route("/captcha-request-new", methods=["POST"])
def captcha_request_new():
    """Startet einen neuen Cookie-Refresh im Hintergrund (triggert neues Captcha)."""
    if _captcha_request_active or _login_attempt_lock.locked():
        return jsonify({
            "ok": True,
            "message": "Login-/Captcha-Anforderung läuft bereits",
        })

    # Prüfen ob FlareSolverr + Credentials konfiguriert sind
    username = _config.get("turktorrent_username", "")
    password = _config.get("turktorrent_password", "")
    flaresolverr_url = _config.get("flaresolverr_url", "")
    if not username or not password:
        return jsonify({"ok": False, "error": "TurkTorrent Username/Passwort nicht konfiguriert"})
    if not flaresolverr_url:
        return jsonify({"ok": False, "error": "FlareSolverr URL nicht konfiguriert"})

    # Cookie-Refresh in Background-Thread starten
    def _bg_refresh():
        with _cookie_refresh_lock:
            _do_cookie_refresh(force_login=True)

    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Neuer Login gestartet – Captcha erscheint gleich"})


@app.route("/captcha-status")
def captcha_status():
    """Gibt den aktuellen Captcha-Status zurück."""
    response = jsonify({
        "active": _captcha_request_active,
        "waiting": _captcha_request_active and not _pending_captcha_event.is_set(),
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  Türk ARR Bridge - Torznab Proxy")
    logger.info(f"  Bridge Port:    {BRIDGE_PORT}")
    logger.info(f"  Upstream:       {UPSTREAM_TORZNAB_URL}")
    logger.info(f"  Sonarr:         {SONARR_URL}")
    logger.info(f"  Radarr:         {RADARR_URL}")
    logger.info("=" * 60)

    # Initiales Cache-Laden
    refresh_title_cache(title_cache)

    # Cookie-Refresh Thread starten
    _start_cookie_refresh_thread()

    # GitHub Update-Check Thread starten
    _start_update_check_thread()

    app.run(host=BRIDGE_HOST, port=BRIDGE_PORT, debug=(LOG_LEVEL == "DEBUG"), threaded=True, use_reloader=False)
