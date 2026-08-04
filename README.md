# 🇹🇷 Türk ARR Bridge

**A Torznab proxy that automatically translates international series titles into Turkish originals — so Sonarr & Radarr can actually find Turkish torrents.**

[![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)](https://www.docker.com/)
[![Python](https://img.shields.io/badge/Python-3.12-green?logo=python)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Optimized for TürkTorrent](https://img.shields.io/badge/Optimized%20for-TürkTorrent-red)](https://turktorrent.us)

---

## The Problem

Sonarr and Radarr only know Turkish series by their **international title** (as registered on TheTVDB). Turkish torrent indexers — especially **TurkTorrent.us** — list those same series exclusively under the **Turkish original title**.

| Sonarr searches for | TürkTorrent only knows |
|---|---|
| `Deeply` | `İlk ve Son` |
| `Persona` | `Şahsiyet` |
| `Innocent` | `Masum` |
| `Resurrection: Ertuğrul` | `Diriliş: Ertuğrul` |
| `The Pit` | `Çukur` |
| `Family Secrets` | `Yargı` |

**Without this bridge:** 0 results for almost every Turkish series or movie.

---

## The Solution

```
┌──────────┐     ┌──────────────────────┐     ┌─────────┐     ┌──────────────┐
│  Sonarr  │────▶│  Türk ARR Bridge     │────▶│ Jackett │────▶│ TürkTorrent  │
│  Radarr  │     │  Port 9696           │     │         │     │  (or other)  │
└──────────┘     └──────────────────────┘     └─────────┘     └──────────────┘
                  Receives: "Deeply"
                  → expands to:
                    "Deeply"
                    "Ilk Ve Son"      ← ASCII variant
                    "İlk ve Son"      ← Turkish original
```

The bridge:
1. **Intercepts** all Torznab search requests from Sonarr/Radarr
2. **Looks up** the Turkish original title via Sonarr/Radarr API → TVDB
3. **Searches** with all title variants including ASCII transliterations
4. **Learns** new mappings automatically and stores them persistently
5. **BoxSet fallback:** if no season pack is found → searches for complete series BoxSets and sends them directly to qBittorrent
6. **Returns** all merged results as standard Torznab XML

---

## Features

- 🔍 **Automatic title translation** — International ↔ Turkish (via TVDB)
- 🧠 **Self-learning** — learns new titles from TVDB lookups and persists them
- 📦 **BoxSet fallback** — auto-downloads complete series BoxSets via qBittorrent when no season pack exists
- 🔄 **Version-robust qBittorrent WebAPI** — supports legacy responses as well as qBittorrent 5.2+ (`204` responses and port-specific session cookies)
- 🩺 **Sonarr/Radarr indexer self-healing** — safely clears persistent ARR backoff after Jackett/tracker recovery
- 📱 **Tailscale-safe Telegram captcha** — optional external URL plus separate LAN/external links in each bot message
- 🎬 **Quality prioritization** — 2160p > 1080p > 720p > SD, H.265 bonus
- ✏️ **Title rewrite** — rewrites torrent titles in XML so Radarr/Sonarr can match Turkish titles
- 📨 **Telegram notifications** — for automatic BoxSet downloads
- 🌐 **Web GUI** — Dashboard, Config, Search tester, Mappings, Logs — with 🇩🇪 🇺🇸 🇹🇷 language switcher
- 💾 **Backup & Restore** — export/import full config as JSON via the GUI
- 🔒 **GUI Auth** — optional Basic Auth protection for the web interface
- 🎵 **Header music** — optional background music in the GUI
- 🐳 **Docker-ready** — runs as a container on any NAS or server
- 🇹🇷 **Optimized for TürkTorrent** — built and tested against [turktorrent.us](https://turktorrent.us)

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/SubZ4242/turk-arr-bridge.git
cd turk-arr-bridge
```

### 2. Create your configuration

```bash
cp .env.example .env
nano .env   # fill in your URLs and API keys
```

### 3. Start the bridge

```bash
chmod +x deploy.sh
./deploy.sh
```

Or manually (prefix with `sudo` on TrueNAS):
```bash
sudo docker compose up -d --build
```

> 💡 **TrueNAS users:** Always use `sudo docker ...` — the `truenas_admin` user does not have direct Docker socket access.

### Portainer Stack (TrueNAS / NAS)

> ⚠️ Portainer does **not** support `env_file` or `build:` in stacks deployed from a Git URL.  
> You must first build the image directly on the NAS via SSH, then deploy via Portainer.

**Step 1 – build the image on your NAS via SSH:**
```bash
# On TrueNAS and most NAS systems, prefix docker commands with sudo
ssh truenas_admin@your-nas-ip
git clone https://github.com/SubZ4242/turk-arr-bridge.git
cd turk-arr-bridge
sudo docker build -t turk-arr-bridge:latest .
```

**Step 2 – paste this into Portainer → Stacks → Add Stack → Web editor:**

```yaml
version: "3.8"
services:
  turk-arr-bridge:
    image: turk-arr-bridge:latest
    container_name: turk-arr-bridge
    restart: unless-stopped
    ports:
      - "9696:9696"
    environment:
      SONARR_URL: "http://YOUR-NAS:8989"
      SONARR_API_KEY: "your_sonarr_api_key"
      RADARR_URL: "http://YOUR-NAS:7878"
      RADARR_API_KEY: "your_radarr_api_key"
      JACKETT_URL: "http://YOUR-NAS:9117"
      JACKETT_API_KEY: "your_jackett_api_key"
      UPSTREAM_TORZNAB_URL: "http://YOUR-NAS:9117/api/v2.0/indexers/YOUR_INDEXER/results/torznab/"
      QBIT_URL: "http://YOUR-NAS:8080"
      QBIT_USER: "admin"
      QBIT_PASS: "your_password"
      TELEGRAM_BOT_TOKEN: ""
      TELEGRAM_CHAT_ID: ""
      BRIDGE_PORT: "9696"
      CACHE_TTL_SECONDS: "300"
      LOG_LEVEL: "INFO"
    volumes:
      - /your/config/path:/config
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9696/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```



In Sonarr/Radarr → Settings → Indexers → edit your Torznab indexer:

| Setting | Before | After |
|---|---|---|
| URL | `http://your-nas:9117/api/v2.0/indexers/.../torznab/` | `http://your-nas:9696/torznab` |
| API Key | *(unchanged)* | *(unchanged)* |

---

## Configuration

All settings can be configured via:
- **`.env` file** (at startup)
- **Environment variables** (Docker / Portainer)
- **Web GUI** at `http://your-host:9696/gui` (at runtime, saved to `/config/bridge_config.json`)

| Variable | Description | Example |
|---|---|---|
| `SONARR_URL` | Sonarr address | `http://nas:8989` |
| `SONARR_API_KEY` | Sonarr API key | |
| `RADARR_URL` | Radarr address | `http://nas:7878` |
| `RADARR_API_KEY` | Radarr API key | |
| `JACKETT_URL` | Jackett address | `http://nas:9117` |
| `JACKETT_API_KEY` | Jackett API key | |
| `UPSTREAM_TORZNAB_URL` | Full Torznab URL of your indexer | |
| `QBIT_URL` | qBittorrent address | `http://nas:8080` |
| `QBIT_USER` | qBittorrent username | |
| `QBIT_PASS` | qBittorrent password | |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) | |
| `TELEGRAM_CHAT_ID` | Telegram group/channel ID (optional) | |
| `BRIDGE_PORT` | Bridge port | `9696` |
| `CACHE_TTL_SECONDS` | Cache duration in seconds | `300` |
| `LOG_LEVEL` | Log verbosity | `INFO` |
| `ARR_INDEXER_AUTO_HEAL` | Re-test this bridge's ARR indexers after verified tracker recovery | `true` |
| `ARR_INDEXER_HEAL_INTERVAL_MINUTES` | Self-healing check interval (minimum 5 minutes) | `15` |
| `BRIDGE_EXTERNAL_URL` | Optional URL reachable externally; when set, Telegram sends both automatic LAN and external links | `https://nas.example.ts.net` |

---

## Endpoints

| Endpoint | Description |
|---|---|
| `http://host:9696/torznab` | Torznab proxy (for Sonarr/Radarr) |
| `http://host:9696/gui` | Web GUI |
| `http://host:9696/health` | Health check |
| `http://host:9696/mappings` | All title mappings as JSON |

---

## Web GUI

The bridge ships with a full web interface at `http://your-host:9696/gui`:

- 🏠 **Dashboard** — connection status, statistics
- 🛠️ **Connections** — configure and test Sonarr, Radarr, Jackett, qBittorrent
- 🏛️ **Tuning** — BoxSet strategy and quality settings
- ⚙️ **System** — GUI auth (Basic Auth), Backup & Restore (JSON export/import)
- 📨 **Telegram** — notification settings
- 🔍 **Search tester** — see how a title gets resolved and what results are found
- 📖 **Title Mappings** — browse all known translations
- 🧠 **Learned** — manage auto-learned mappings
- 📋 **Logs** — live log stream
- 🇩🇪 🇺🇸 🇹🇷 **Language switcher** — German, English, Turkish UI

---

## BoxSet Fallback

When Sonarr finds no results for a season, the bridge automatically searches for **complete series BoxSets** (e.g. titles containing "Tüm Sezon", "Komple", "Bütün Bölümler", "S01-S05", "Complete Series"). These are sent directly to qBittorrent — Sonarr receives normal season-pack results if any were found.

**Selection strategy** (configurable in the GUI):
- 🎬 **Quality** (default): 2160p > 1080p > 720p > SD, H.265 bonus
- 🌱 **Seeders**: most seeders first for faster downloads

---

## TürkTorrent Setup

This bridge is specifically optimized for **[TürkTorrent](https://turktorrent.us)** — the largest Turkish torrent tracker. Add TürkTorrent to Jackett, then use the Jackett Torznab URL as your `UPSTREAM_TORZNAB_URL`. The bridge handles all title mismatches automatically.

---

## Built-in Title Mappings

The bridge includes a predefined mapping database for popular Turkish series including:

`Diriliş: Ertuğrul`, `Çukur`, `Yargı`, `Masum`, `Şahsiyet`, `Fatih Harbiye`, `Ezel`, `Kurtlar Vadisi`, `İçerde`, `Söz`, `Sen Çal Kapımı`, `Kuruluş: Osman` and many more.

New titles are learned automatically via TVDB lookups.

---

## Requirements

- Docker + Docker Compose
- Sonarr v3+ / Radarr v3+
- Jackett (with a Turkish indexer, e.g. TürkTorrent)
- qBittorrent (optional, for BoxSet auto-download)

The qBittorrent integration keeps the complete cookie session returned by the
WebAPI and verifies it through an authenticated API call. It deliberately does
not depend on a fixed cookie name or on the legacy `Ok.` login response, so
qBittorrent upgrades can change those implementation details without breaking
the bridge.

---

## License

MIT License — see [LICENSE](LICENSE)

---

*Built for the Turkish home cinema setup. Contributions welcome!* 🇹🇷

---
