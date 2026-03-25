#!/bin/bash
#
# Türk ARR Bridge - Deployment Script für TrueNAS
#
# Dieses Script:
# 1. Kopiert die Bridge-Dateien auf das NAS
# 2. Baut den Docker-Container
# 3. Startet die Bridge
# 4. Konfiguriert Sonarr & Radarr um die Bridge statt Jackett direkt zu nutzen
#

set -euo pipefail

# ============================================================
# Konfiguration
# ============================================================

NAS_HOST="192.168.178.76"
NAS_USER="truenas_admin"
NAS_DEPLOY_PATH="/root/turk-arr-bridge"

BRIDGE_PORT="9696"
BRIDGE_URL="http://${NAS_HOST}:${BRIDGE_PORT}"

SONARR_URL="http://${NAS_HOST}:30199"
SONARR_API_KEY="e4a818c5f4b545f98704459811912fa7"

RADARR_URL="http://${NAS_HOST}:30095"
RADARR_API_KEY="157cffb2534a4171a84e36df111307e0"

JACKETT_API_KEY="9kyp7kl9ofqxa8r02mkvpr5msykqytzy"

# Farben
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ============================================================
# Schritt 1: Dateien auf NAS kopieren
# ============================================================

echo ""
echo "=============================================="
echo "  Türk ARR Bridge - NAS Deployment"
echo "=============================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

log_info "Kopiere Dateien auf NAS (${NAS_HOST})..."

ssh "${NAS_USER}@${NAS_HOST}" "mkdir -p ${NAS_DEPLOY_PATH}" 2>/dev/null || {
    log_error "SSH-Verbindung fehlgeschlagen! Bitte sicherstellen:"
    echo "  1. SSH-Zugang zu ${NAS_HOST} ist eingerichtet"
    echo "  2. Nutzer '${NAS_USER}' existiert und hat Zugang"
    echo "  3. ssh-copy-id ${NAS_USER}@${NAS_HOST} ausführen für passwordless login"
    exit 1
}

scp -q "${SCRIPT_DIR}/bridge.py" "${NAS_USER}@${NAS_HOST}:${NAS_DEPLOY_PATH}/"
scp -q "${SCRIPT_DIR}/requirements.txt" "${NAS_USER}@${NAS_HOST}:${NAS_DEPLOY_PATH}/"
scp -q "${SCRIPT_DIR}/Dockerfile" "${NAS_USER}@${NAS_HOST}:${NAS_DEPLOY_PATH}/"
scp -q "${SCRIPT_DIR}/docker-compose.yml" "${NAS_USER}@${NAS_HOST}:${NAS_DEPLOY_PATH}/"

log_ok "Dateien kopiert nach ${NAS_DEPLOY_PATH}"

# ============================================================
# Schritt 2: Docker Container bauen und starten
# ============================================================

log_info "Baue und starte Docker Container auf NAS..."

ssh "${NAS_USER}@${NAS_HOST}" bash -s <<'REMOTE_SCRIPT'
cd /root/turk-arr-bridge

# Alten Container stoppen falls vorhanden
docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true

# Container bauen und starten
if command -v docker compose &> /dev/null; then
    docker compose up -d --build
elif command -v docker-compose &> /dev/null; then
    docker-compose up -d --build
else
    echo "FEHLER: docker compose nicht gefunden!"
    exit 1
fi

# Warte auf Healthcheck
echo "Warte auf Bridge-Start..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:9696/health > /dev/null 2>&1; then
        echo "Bridge ist bereit!"
        break
    fi
    sleep 1
done

curl -s http://localhost:9696/health | python3 -m json.tool 2>/dev/null || echo "Health-Check pending..."
REMOTE_SCRIPT

log_ok "Docker Container läuft"

# ============================================================
# Schritt 3: Bridge lokal testen
# ============================================================

log_info "Teste Bridge-Verbindung..."

sleep 3

# Health Check
HEALTH=$(curl -sf "${BRIDGE_URL}/health" 2>/dev/null) || {
    log_error "Bridge nicht erreichbar auf ${BRIDGE_URL}/health"
    log_warn "Container-Logs prüfen mit: ssh ${NAS_USER}@${NAS_HOST} 'docker logs turk-arr-bridge'"
    exit 1
}

log_ok "Bridge Health Check erfolgreich"
echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"

# Test-Suche
log_info "Teste Suche: 'Deeply' → sollte türkische Ergebnisse finden..."
RESULT=$(curl -sf "${BRIDGE_URL}/torznab?apikey=${JACKETT_API_KEY}&t=search&q=Deeply" 2>/dev/null)
ITEM_COUNT=$(echo "$RESULT" | grep -c '<item>' || echo "0")
log_ok "Suche 'Deeply' ergab ${ITEM_COUNT} Ergebnisse (via Bridge mit türkischem Alias)"

# ============================================================
# Schritt 4: Sonarr Indexer aktualisieren
# ============================================================

echo ""
log_info "Konfiguriere Sonarr Indexer..."

# Aktuellen Torznab-Indexer in Sonarr finden und die URL auf die Bridge umbiegen
INDEXERS=$(curl -sf "${SONARR_URL}/api/v3/indexer?apikey=${SONARR_API_KEY}" 2>/dev/null)

# Finde den TürkTorrent Indexer
TURK_INDEXER_ID=$(echo "$INDEXERS" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for idx in data:
    for f in idx.get('fields', []):
        if f.get('name') == 'baseUrl' and 'turktorrent' in str(f.get('value', '')):
            print(idx['id'])
            break
" 2>/dev/null)

if [ -n "$TURK_INDEXER_ID" ]; then
    log_info "TürkTorrent Indexer gefunden (ID: ${TURK_INDEXER_ID}), aktualisiere URL..."
    
    # Hole aktuelle Konfiguration
    INDEXER_CONFIG=$(curl -sf "${SONARR_URL}/api/v3/indexer/${TURK_INDEXER_ID}?apikey=${SONARR_API_KEY}")
    
    # Update die baseUrl auf die Bridge
    UPDATED_CONFIG=$(echo "$INDEXER_CONFIG" | python3 -c "
import json, sys
config = json.load(sys.stdin)
for f in config.get('fields', []):
    if f.get('name') == 'baseUrl':
        f['value'] = 'http://${NAS_HOST}:${BRIDGE_PORT}/torznab'
print(json.dumps(config))
")
    
    # Sende Update
    HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" \
        -X PUT \
        "${SONARR_URL}/api/v3/indexer/${TURK_INDEXER_ID}?apikey=${SONARR_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "$UPDATED_CONFIG" 2>/dev/null)
    
    if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
        log_ok "Sonarr Indexer URL aktualisiert auf Bridge (${BRIDGE_URL}/torznab)"
    else
        log_warn "Sonarr Indexer Update HTTP $HTTP_CODE - manuell prüfen!"
        log_warn "Manuell: Sonarr → Settings → Indexers → Torznab → URL ändern auf:"
        echo "         ${BRIDGE_URL}/torznab"
    fi
else
    log_warn "Kein TürkTorrent Indexer in Sonarr gefunden."
    log_info "Bitte manuell hinzufügen:"
    echo "  Sonarr → Settings → Indexers → + → Torznab"
    echo "  URL:    ${BRIDGE_URL}/torznab"
    echo "  API Key: ${JACKETT_API_KEY}"
fi

# ============================================================
# Schritt 5: Radarr Indexer aktualisieren
# ============================================================

log_info "Konfiguriere Radarr Indexer..."

RADARR_INDEXERS=$(curl -sf "${RADARR_URL}/api/v3/indexer?apikey=${RADARR_API_KEY}" 2>/dev/null)

RADARR_TURK_ID=$(echo "$RADARR_INDEXERS" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for idx in data:
    for f in idx.get('fields', []):
        if f.get('name') == 'baseUrl' and 'turktorrent' in str(f.get('value', '')):
            print(idx['id'])
            break
" 2>/dev/null)

if [ -n "$RADARR_TURK_ID" ]; then
    log_info "TürkTorrent Indexer in Radarr gefunden (ID: ${RADARR_TURK_ID}), aktualisiere URL..."
    
    RADARR_INDEXER_CONFIG=$(curl -sf "${RADARR_URL}/api/v3/indexer/${RADARR_TURK_ID}?apikey=${RADARR_API_KEY}")
    
    RADARR_UPDATED=$(echo "$RADARR_INDEXER_CONFIG" | python3 -c "
import json, sys
config = json.load(sys.stdin)
for f in config.get('fields', []):
    if f.get('name') == 'baseUrl':
        f['value'] = 'http://${NAS_HOST}:${BRIDGE_PORT}/torznab'
print(json.dumps(config))
")
    
    HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" \
        -X PUT \
        "${RADARR_URL}/api/v3/indexer/${RADARR_TURK_ID}?apikey=${RADARR_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "$RADARR_UPDATED" 2>/dev/null)
    
    if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
        log_ok "Radarr Indexer URL aktualisiert auf Bridge (${BRIDGE_URL}/torznab)"
    else
        log_warn "Radarr Indexer Update HTTP $HTTP_CODE - manuell prüfen!"
    fi
else
    log_warn "Kein TürkTorrent Indexer in Radarr gefunden."
    log_info "Bitte manuell hinzufügen:"
    echo "  Radarr → Settings → Indexers → + → Torznab"
    echo "  URL:    ${BRIDGE_URL}/torznab"
    echo "  API Key: ${JACKETT_API_KEY}"
fi

# ============================================================
# Zusammenfassung
# ============================================================

echo ""
echo "=============================================="
echo "  Deployment abgeschlossen!"
echo "=============================================="
echo ""
echo "  Bridge URL:    ${BRIDGE_URL}"
echo "  Health:        ${BRIDGE_URL}/health"
echo "  Mappings:      ${BRIDGE_URL}/mappings"
echo "  Test-Suche:    ${BRIDGE_URL}/test/Deeply"
echo ""
echo "  Sonarr Indexer: URL sollte jetzt auf Bridge zeigen"
echo "  Radarr Indexer: URL sollte jetzt auf Bridge zeigen"
echo ""
echo "  Wie es funktioniert:"
echo "  ┌─────────┐    ┌───────────────┐    ┌──────────┐    ┌────────────┐"
echo "  │ Sonarr  │───▶│ TürkARRBridge │───▶│ Jackett  │───▶│ TürkTorrent│"
echo "  │ Radarr  │    │ Port ${BRIDGE_PORT}      │    │ Port 30196│    │            │"
echo "  └─────────┘    └───────────────┘    └──────────┘    └────────────┘"
echo "                  Suche 'Deeply'                       Hat nur"
echo "                  → 'Deeply'                           'Ilk ve Son'"
echo "                  + 'Ilk ve Son'"
echo "                  + 'Ilk Ve Son'"
echo ""
log_warn "ZUSÄTZLICHES PROBLEM ENTDECKT:"
echo "  Root folder '/data/Medien/Serien TR' was not found!"
echo "  Serien-Pfad zeigt auf /data2/... - Sonarr erwartet /data/..."
echo "  → In Sonarr prüfen: Settings → Media Management → Root Folders"
echo ""
