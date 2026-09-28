#!/usr/bin/env bash
# ============================================================
#  Ningsi Studio smoke test (Linux / macOS)
#  Requires a running service. Usage:
#    ./scripts/smoke.sh [base_url] [participant] [speed]
# ============================================================
set -uo pipefail

BASE="${1:-http://127.0.0.1:8765}"
PARTICIPANT="${2:-p99}"
SPEED="${3:-0.05}"
TIMEOUT_SEC=600

# 与后端 schemas.normalize_public_id 对齐
PARTICIPANT="$(printf '%s' "$PARTICIPANT" | tr 'A-Z' 'a-z' | tr -d ' ' | sed 's/^sub-//')"
case "$PARTICIPANT" in
    [a-z][a-z][a-z][0-9]*|[a-z][a-z][0-9]*|[a-z][0-9]*|[0-9]*) : ;;
    *) echo "  [x] participant 编号不合法：'$2'（应形如 p01 / sub-p01）" >&2; exit 1 ;;
esac

failures=0
step() { printf '  -> %s\n' "$1"; }
ok()   { printf '  [ok] %s %s\n' "$1" "${2:-}"; }
bad()  { printf '  [x] %s %s\n' "$1" "${2:-}"; failures=$((failures + 1)); }

need() { # need <label> <haystack> <needle>
    case "$2" in
        *"$3"*) ok "$1" ;;
        *) bad "$1" "expected to contain: $3" ;;
    esac
}

echo ""
echo "=== Ningsi Studio smoke test (bash) ==="
echo "  target: $BASE"

if ! health="$(curl -fsS --max-time 10 "$BASE/api/health")"; then
    echo "  [x] service not reachable: $BASE"
    echo "  start it first:  ./run.sh serve"
    exit 2
fi
need "health status" "$health" '"status":"ok"'
need "engine spec" "$health" 'welch-v1'

config="$(curl -fsS "$BASE/api/config")"
need "config window_sec" "$config" '"window_sec":4.0'
devices="$(curl -fsS "$BASE/api/devices?probe=0.3")"
need "device list" "$devices" 'sim-bsense'
openapi="$(curl -fsS "$BASE/api/openapi.json")"
need "openapi" "$openapi" '"title"'

step "create subject sub-$PARTICIPANT"
curl -fsS -X POST "$BASE/api/subjects" -H 'Content-Type: application/json' \
    -d "{\"public_id\":\"$PARTICIPANT\",\"label\":\"smoke-test\"}" >/dev/null \
    || echo "  [!] subject may already exist; continuing"

step "create session (time_scale=$SPEED, fast demo mode)"
created="$(curl -fsS -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' \
    -d "{\"participant\":\"$PARTICIPANT\",\"device\":\"sim-bsense\",\"time_scale\":$SPEED,\"training_mode\":\"quick\"}")"
uuid="$(printf '%s' "$created" | sed -n 's/.*"uuid":"\([0-9a-f]\{32\}\)".*/\1/p')"
if [ -z "$uuid" ]; then
    bad "session created" "no uuid in response: $created"
    exit 1
fi
ok "session created" "uuid=$uuid"

deadline=$(( $(date +%s) + TIMEOUT_SEC ))
status="running"
while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 2
    detail="$(curl -fsS "$BASE/api/sessions/$uuid")"
    status="$(printf '%s' "$detail" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')"
    phase="$(printf '%s' "$detail" | sed -n 's/.*"phase":"\([a-z_]*\)".*/\1/p')"
    printf '     phase: %s\n' "$phase"
    case "$status" in
        done|failed|cancelled) break ;;
    esac
done

if [ "$status" = "done" ]; then ok "session finished" "status=done"; else bad "session finished" "status=$status"; fi

if [ "$status" = "done" ]; then
    report="$(curl -fsS "$BASE/api/sessions/$uuid/report")"
    need "report spec" "$report" 'joint-assessment-v1'
    need "assessment conclusion" "$report" '"conclusion"'
    need "behavior sart 180 trials" "$report" '"trials":180'
    heat="$(curl -fsS "$BASE/api/sessions/$uuid/heatmap")"
    need "heatmap cells" "$heat" '"cells"'
    trend="$(curl -fsS "$BASE/api/reports/trend?field=focus&period=week")"
    need "trend points" "$trend" '"points"'
    arts="$(curl -fsS "$BASE/api/sessions/$uuid/artifacts")"
    for kind in report_md report_json heatmap_svg trend_svg; do
        need "artifact $kind" "$arts" "\"kind\":\"$kind\""
    done
    zip_bytes="$(curl -fsS "$BASE/api/sessions/$uuid/export.zip" | wc -c | tr -d ' ')"
    if [ "${zip_bytes:-0}" -gt 500 ]; then ok "export zip" "bytes=$zip_bytes"; else bad "export zip" "bytes=$zip_bytes"; fi
fi

code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/sessions/ffffffffffffffffffffffffffffffff")"
if [ "$code" = "404" ]; then ok "unknown session -> 404"; else bad "unknown session -> 404" "code=$code"; fi

echo ""
if [ "$failures" -eq 0 ]; then
    echo "SMOKE PASSED"
    exit 0
fi
echo "SMOKE FAILED: $failures check(s)"
exit 1
