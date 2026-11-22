#!/usr/bin/env bash
# run_mock_test.sh - formal fixture test for pms_trace.sh (open-source build)
# SPDX-License-Identifier: MIT  -  Copyright (c) 2026 Rodrigo Ramos
#
# HOW TO RUN (macOS iTerm, from your home folder):
#   one-time:  brew install bash coreutils python
#   files:     keep the repo layout (pms_trace.sh, connector_kb.json, backends/ next to this script)
#   run:       cd ~/pms-trace && /opt/homebrew/bin/bash ./run_mock_test.sh   # Apple Silicon
#              (Intel Homebrew: /usr/local/bin/bash; MacPorts: /opt/local/bin/bash)
# HOW TO RUN (Linux):  bash ./run_mock_test.sh
#
# What it does: builds a throw-away sandbox with a fixture log archive, points the REAL
# reference backends (backends/s3search.py, s3grep.py) at it, runs the real pms_trace.sh
# and ASSERTS on the results - no network needed. Exit 0 = PASS.
#   trace     : delivered booking, failing booking (conversion), co-tenant decoy, KB
#               write-back + self-learning (discover -> relearn), ssh:// and s3:// shims
#   backends  : direct contract checks (ls/grep/query, .gz, unsafe input, transports)
#   fidelity  : offline docs (all PRESERVED), no docs (honest 'unavailable'), and
#               trace+fidelity with docs auto-fetched via harvested keys (verdicts)
#   ari       : manual GUI change attributed to a user (goal rate-mismatch) and a
#               PMS-push origin with the bridge ingest leg (region rb)
#   guiattrib : GUI change -> flowToken -> actor email/IP, session read, audit write
#   kb-test   : every KB module vs the catalog (LIVE / STALE?)
#   bridgeflush: goal bridge-flush - sub-leg isolation, culprit room rate derived
#   contracts : goal input contract refusal (exit 2, zero scans), unknown task name
#               (named with the valid list; the remaining tasks still run)
#
# OPTIONS (flags or env vars; with no flag/env set you are asked at the console):
#   -s, --save [FILE]  save the full console output to FILE (default: ./test-reports/
#                      FULL_TEST_REPORT_<UTC>.txt in the folder you run from). Local
#                      paths are redacted (<sandbox>, ~) so the report is safe to share.  env SAVE=1 / SAVE_PATH=FILE
#   -v, --verbose      per-scenario function report (resolution, backend calls, join keys,
#                      verdict.json, REPORT.md, NEXT_ACTIONS.md)                           env VERBOSE=1
#   -k, --keep         keep the throw-away sandbox (raw run dirs) for inspection          env KEEP=1
#   -y, --no-prompt    never ask; unset options default to off (CI / non-interactive)
#   -h, --help
set -u
# ---- options: flags > env > console prompt (prompt only on an interactive terminal) ----
NO_PROMPT=0
while [ $# -gt 0 ]; do
  case "$1" in
    -s|--save)    SAVE=1; if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then SAVE_PATH="$2"; shift; fi ;;
    --save=*)     SAVE=1; SAVE_PATH="${1#--save=}" ;;
    -v|--verbose) VERBOSE=1 ;;
    -k|--keep)    KEEP=1 ;;
    -y|--no-prompt) NO_PROMPT=1 ;;
    -h|--help)    sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 2 ;;
  esac; shift
done
ask() { # var question - y/N prompt, only when stdin is a terminal and the var is unset
  local var="$1" ans
  [ -n "${!var+x}" ] && return 0
  if [ "$NO_PROMPT" = "0" ] && [ -t 0 ]; then
    read -r -p "$2 [y/N]: " ans
    case "$ans" in [yY]|[yY][eE][sS]) printf -v "$var" 1 ;; *) printf -v "$var" 0 ;; esac
  else printf -v "$var" 0; fi
}
ask SAVE    "Save the full test report to a file in this folder?"
ask VERBOSE "Verbose mode (function-by-function report per scenario)?"
ask KEEP    "Keep the sandbox (raw run folders) after the run?"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pick() { [ -f "$SCRIPT_DIR/$1" ] && echo "$SCRIPT_DIR/$1" || { [ -f "$HOME/$1" ] && echo "$HOME/$1"; }; }
KBSRC="$(pick connector_kb.json)"
if [ -x "$SCRIPT_DIR/.venv/bin/pms-trace" ]; then
  PT_BIN="$SCRIPT_DIR/.venv/bin/pms-trace"
elif command -v pms-trace >/dev/null 2>&1; then
  PT_BIN="$(command -v pms-trace)"
else
  echo "FAIL: pms-trace CLI not found (.venv/bin/pms-trace or on PATH). Run: pip install ."
  exit 2
fi

# ---- prerequisites ------------------------------------------------------------
if [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
  echo "FAIL: bash >= 4 required (found $BASH_VERSION). brew install bash, then run with /opt/homebrew/bin/bash"; exit 2; fi
command -v python3 >/dev/null || { echo "FAIL: python3 not found (brew install python)"; exit 2; }
true
[ -n "$KBSRC" ] || { echo "FAIL: connector_kb.json not found next to this script or in ~"; exit 2; }
python3 -m json.tool "$KBSRC" >/dev/null || { echo "FAIL: connector_kb.json is not valid JSON"; exit 2; }
"$PT_BIN" --version >/dev/null || { echo "FAIL: pms-trace --version failed"; exit 2; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pms_mock.XXXXXX")"
LAUNCH_DIR="$PWD"
FULL_REPORT=""
if [ "${SAVE:-0}" = "1" ]; then
  FULL_REPORT="${SAVE_PATH:-$LAUNCH_DIR/test-reports/FULL_TEST_REPORT_$(date -u +%Y%m%dT%H%M%SZ).txt}"
  case "$FULL_REPORT" in /*) ;; *) FULL_REPORT="$LAUNCH_DIR/$FULL_REPORT" ;; esac
  mkdir -p "$(dirname "$FULL_REPORT")" || { echo "FAIL: cannot create $(dirname "$FULL_REPORT")"; exit 2; }
  : > "$FULL_REPORT.raw" || { echo "FAIL: cannot write $FULL_REPORT"; exit 2; }
  exec 3>&1 4>&2
  exec > >(tee -a "$FULL_REPORT.raw") 2>&1
  TEE_PID=$!
fi
finish() { # flush the tee, write the redacted report, then clean the sandbox
  local rc=$?
  if [ -n "$FULL_REPORT" ]; then
    exec 1>&3 2>&4 3>&- 4>&-
    wait "$TEE_PID" 2>/dev/null
    python3 - "$FULL_REPORT.raw" "$FULL_REPORT" "$ROOT" "$HOME" "$SCRIPT_DIR" <<'PYRED'
import sys
raw, out, root, home, sdir = sys.argv[1:6]
t = open(raw, errors="replace").read()
for a, b in ((root.replace("//", "/"), "<sandbox>"), (root, "<sandbox>"), (sdir, "<repo>"), (home, "~")):
    if a: t = t.replace(a, b)
t = t.replace("//pms_mock", "/pms_mock")
open(out, "w").write(t)
PYRED
    rm -f "$FULL_REPORT.raw"
    echo "full test report saved (paths redacted): $FULL_REPORT"
  fi
  if [ "${KEEP:-0}" = "1" ]; then echo "sandbox kept: $ROOT"; else rm -rf "$ROOT"; fi
  exit $rc
}
trap finish EXIT
BIN="$ROOT/bin"; mkdir -p "$BIN"
if [ "${KEEP:-0}" = "1" ]; then
  KEEP_LOG="$ROOT/FULL_TEST_REPORT.txt"   # raw (unredacted) copy inside the kept sandbox
  exec > >(tee -a "$KEEP_LOG") 2>&1
fi
# GNU tool shims: macOS BSD date/timeout/stat/sed/readlink lack the GNU flags the tool uses.
for t in date timeout stat sed readlink seq sort; do
  command -v "g$t" >/dev/null 2>&1 && ln -sf "$(command -v "g$t")" "$BIN/$t"
done
PATH="$BIN:$PATH"
date -u -d "2026-01-01 00:00:00" +%s >/dev/null 2>&1 || { echo "FAIL: GNU date required (brew install coreutils)"; exit 2; }
command -v timeout >/dev/null || { echo "FAIL: 'timeout' missing (brew install coreutils)"; exit 2; }
echo "== env: $(bash --version | head -1)"
echo "== env: $(python3 --version) | date=$(command -v date) | trace=$PT_BIN | kb=$KBSRC"

# ---- fixture dataset: what the log archive would contain ------------------------
# One JSON object per line; "_sys" / "_comp" route it to an app. Everything else is the
# raw log line the backend returns. All ids are synthetic.
FIX="$ROOT/fixture.jsonl"
cat > "$FIX" <<'FIXEOF'
{"_sys":"cm-ota2","_comp":"booking-gateway","@timestamp":"2026-01-10T12:03:11.402Z","level":"INFO","msg":"make_reservation received","reservationId":"OTA2-1000000001","sourceAgentCode":"OTA2","echoToken":"ECHO-7f3a9c21","flowToken":"0a1b2c3d-1111-4222-8333-944455556666","propertyUuid":"11111111-2222-4333-8444-555555555555","roomCode":"DBL","ratePlanCode":"BAR"}
{"_sys":"cm-ota2","_comp":"booking-gateway","@timestamp":"2026-01-10T12:03:11.988Z","level":"INFO","msg":"RESPONSE make_reservation","reservationId":"OTA2-1000000001","statusCode":200,"flowToken":"0a1b2c3d-1111-4222-8333-944455556666"}
{"_sys":"cm-ota2","_comp":"booking-processor","@timestamp":"2026-01-10T12:03:12.120Z","level":"INFO","msg":"acceptance persisted","statusCode":200,"flowToken":"0a1b2c3d-1111-4222-8333-944455556666","hubBookingUuid":"9c8b7a65-4321-4fed-8cba-0123456789ab"}
{"_sys":"hub-channel-store","_comp":"booking-api","@timestamp":"2026-01-10T12:03:12.733Z","level":"INFO","msg":"Fetching reservation for delivery","cmBookingId":"OTA2-1000000001","hubBookingUuid":"9c8b7a65-4321-4fed-8cba-0123456789ab","flowToken":"0a1b2c3d-1111-4222-8333-944455556666"}
{"_sys":"pms-hub","_comp":"booking-api","@timestamp":"2026-01-10T12:03:13.050Z","level":"INFO","msg":"pmsBookings upsert","hubBookingUuid":"9c8b7a65-4321-4fed-8cba-0123456789ab","pmsBookingId":"DIR-OTA2-1000000001-1","pmsType":"CLOUDPMS","hubPmsPropertyUuid":"22222222-3333-4444-8555-666666666666"}
{"_sys":"pms-hub","_comp":"pms-booking-events","@timestamp":"2026-01-10T12:03:14.410Z","level":"INFO","msg":"Delivered reservation version 1","hubBookingUuid":"9c8b7a65-4321-4fed-8cba-0123456789ab","status":"Delivered","flowToken":"0a1b2c3d-1111-4222-8333-944455556666"}
{"_sys":"cm-ota2","_comp":"booking-gateway","@timestamp":"2026-01-10T12:41:00.000Z","level":"INFO","msg":"make_reservation received","reservationId":"OTA2-1000000777","sourceAgentCode":"OTA2","flowToken":"ffffffff-0000-4000-8000-000000000777"}
{"_sys":"cm-ota2","_comp":"booking-gateway","@timestamp":"2026-01-10T15:20:01.100Z","level":"INFO","msg":"make_reservation received","reservationId":"OTA2-2000000002","sourceAgentCode":"OTA2","flowToken":"5e5e5e5e-2222-4333-8444-955566667777"}
{"_sys":"cm-ota2","_comp":"booking-gateway","@timestamp":"2026-01-10T15:20:01.600Z","level":"INFO","msg":"RESPONSE make_reservation","reservationId":"OTA2-2000000002","statusCode":200,"flowToken":"5e5e5e5e-2222-4333-8444-955566667777"}
{"_sys":"hub-channel-store","_comp":"booking-api","@timestamp":"2026-01-10T15:20:02.300Z","level":"INFO","msg":"Fetching reservation for delivery","cmBookingId":"OTA2-2000000002","hubBookingUuid":"7d7d7d7d-aaaa-4bbb-8ccc-dddddddddddd","flowToken":"5e5e5e5e-2222-4333-8444-955566667777"}
{"_sys":"pms-hub","_comp":"pms-booking-events","@timestamp":"2026-01-10T15:20:03.900Z","level":"ERROR","msg":"reservation conversion failed","hubBookingUuid":"7d7d7d7d-aaaa-4bbb-8ccc-dddddddddddd","status":"Error","faultCode":"ERR_CONVERSION_UNMAPPED_ROOM_RATE","flowToken":"5e5e5e5e-2222-4333-8444-955566667777"}
{"_sys":"cm-ota2","_comp":"notify-gateway","@timestamp":"2026-01-10T12:03:11.700Z","level":"INFO","msg":"OTA notification payload","echoToken":"ECHO-7f3a9c21","payload":"<OTA_HotelResNotifRQ EchoToken=\"ECHO-7f3a9c21\"><RoomType RoomTypeCode=\"DBL\"/><RatePlan RatePlanCode=\"BAR\"/><Total AmountBeforeTax=\"200.00\" AmountAfterTax=\"220.00\" CurrencyCode=\"EUR\"><Taxes><Tax Amount=\"20.00\" Code=\"VAT\" Type=\"Inclusive\"/></Taxes></Total><TimeSpan Start=\"2026-02-14\" End=\"2026-02-16\"/><GivenName>Alex</GivenName></OTA_HotelResNotifRQ>"}
{"_sys":"pms-hub","_comp":"booking-api","@timestamp":"2026-01-10T12:03:13.300Z","level":"INFO","msg":"PMS document sent","hubBookingUuid":"9c8b7a65-4321-4fed-8cba-0123456789ab","payload":"<Reservation><valueNum>220.00</valueNum><Total CurrencyCode=\"EUR\"/><RoomType RoomTypeCode=\"DBL\"/><RatePlan RatePlanCode=\"BAR\"/><TimeSpan Start=\"2026-02-14\" End=\"2026-02-16\"/><GivenName>Alex</GivenName></Reservation>"}
{"_sys":"cm","_comp":"avail-api-ra","@timestamp":"2026-01-12T09:14:05.000Z","level":"INFO","message":"SET RATE propertyUuid=33333333-4444-4555-8666-777777777777 roomRateKey=RRK-DBL-BAR date=2026-02-14 value=129.00","originSystem":"platform","client":"webcm","flowToken":"3c3c3c3c-1111-4222-8333-944455550003","roomRateKey":"RRK-DBL-BAR"}
{"_sys":"portal-property","_comp":"gui-gateway","@timestamp":"2026-01-12T09:14:04.500Z","level":"INFO","msg":"graphql server operation was successful","flowToken":"3c3c3c3c-1111-4222-8333-944455550003","email":"jane.doe@demo-hotel.test","ipAddress":"198.51.100.10"}
{"_sys":"cm-ota2","_comp":"avail-gateway","@timestamp":"2026-01-12T09:15:20.000Z","level":"INFO","msg":"ARI push rate update listing LA-778899 date 2026-02-14 value 129.00","listingAcctId":"LA-778899","response":"Success","statusCode":200}
{"_sys":"cm","_comp":"avail-api-rb","@timestamp":"2026-01-12T17:40:00.000Z","level":"INFO","message":"SET AVAIL propertyUuid=44444444-5555-4666-8777-888888888888 date=2026-02-20 avail=3","originSystem":"CLOUDPMS","flowToken":"4d4d4d4d-1111-4222-8333-944455550004"}
{"_sys":"pms-cm-bridge","_comp":"avail-sync","@timestamp":"2026-01-12T17:39:30.000Z","level":"INFO","msg":"availability batch received","propertyKey":"PK-445566","originFlowTokens":["4d4d4d4d-1111-4222-8333-944455550004"]}
{"_sys":"hub-channel-link","_comp":"link-api","@timestamp":"2026-01-13T14:22:10.100Z","level":"INFO","msg":"PUT /api/properties/PK-445566/channel-links/55555555-6666-4777-8888-999999999999 200","channelPropertyUuid":"55555555-6666-4777-8888-999999999999","flowToken":"6b6b6b6b-1111-4222-8333-944455550001"}
{"_sys":"portal-property","_comp":"gui-gateway","@timestamp":"2026-01-13T14:22:09.900Z","level":"INFO","msg":"Mutation setChannelProperty","flowToken":"6b6b6b6b-1111-4222-8333-944455550001","email":"sam.ops@demo-hotel.test","kubernetes":{"labels":{"component":"gui-gateway"}}}
{"_sys":"portal-property","_comp":"gui-gateway","@timestamp":"2026-01-13T14:22:10.300Z","level":"INFO","msg":"graphql server operation was successful","flowToken":"6b6b6b6b-1111-4222-8333-944455550001","email":"sam.ops@demo-hotel.test","ipAddress":"198.51.100.23","kubernetes":{"labels":{"component":"gui-gateway"}}}
{"_sys":"platform-session","_comp":"api","@timestamp":"2026-01-13T14:20:00.000Z","level":"INFO","msg":"GET property settings","propertyId":"PK-445566","email":"sam.ops@demo-hotel.test"}
{"_sys":"cm","_comp":"entity-api-ra","@timestamp":"2026-01-13T14:22:11.000Z","level":"INFO","message":"audit: Rate modification applied listing LA-778899 factor changed"}
{"_sys":"cloudpms","_comp":"main-ra","@timestamp":"2026-01-14T10:05:00.000Z","level":"INFO","msg":"Started GET \"/properties/880011/flush?type=all\" request_id=7a7a7a7a-1111-4222-8333-944455550002 user=ops.lead@demo-hotel.test"}
{"_sys":"cloudpms","_comp":"main-ra","@timestamp":"2026-01-14T10:05:01.000Z","level":"INFO","msg":"request_id=7a7a7a7a-1111-4222-8333-944455550002 sub-request type=inventory -> 204 in 120ms"}
{"_sys":"cloudpms","_comp":"main-ra","@timestamp":"2026-01-14T10:05:02.000Z","level":"INFO","msg":"request_id=7a7a7a7a-1111-4222-8333-944455550002 sub-request type=core -> 204 in 80ms"}
{"_sys":"cloudpms","_comp":"main-ra","@timestamp":"2026-01-14T10:05:03.000Z","level":"ERROR","msg":"request_id=7a7a7a7a-1111-4222-8333-944455550002 sub-request hub-pms-sync -> 500 in 900ms"}
{"_sys":"pms-cm-bridge","_comp":"hub-sync-api","@timestamp":"2026-01-14T10:05:02.500Z","level":"ERROR","msg":"setRoomRate failed status=400 property PK-445566 request 7a7a7a7a-1111-4222-8333-944455550002"}
{"_sys":"pms-cm-bridge","_comp":"hub-sync-api","@timestamp":"2026-01-14T10:05:02.900Z","level":"ERROR","msg":"Flushing property data failed PK-445566 request 7a7a7a7a-1111-4222-8333-944455550002"}
{"_sys":"cloudpms","_comp":"entity-api","@timestamp":"2026-01-14T10:05:02.100Z","level":"INFO","msg":"PUT /properties/PK-445566/room-types/RT-DBL 200 9ms"}
{"_sys":"cloudpms","_comp":"entity-api","@timestamp":"2026-01-14T10:05:02.200Z","level":"INFO","msg":"PUT /properties/PK-445566/room-types/RT-DBL/room-rates/RR-1001 200 15ms"}
{"_sys":"cloudpms","_comp":"entity-api","@timestamp":"2026-01-14T10:05:02.300Z","level":"INFO","msg":"PUT /properties/PK-445566/room-types/RT-DBL/room-rates/RR-1002 200 14ms"}
{"_sys":"cloudpms","_comp":"entity-api","@timestamp":"2026-01-14T10:05:02.400Z","level":"WARN","msg":"PUT /properties/PK-445566/room-types/RT-DBL/room-rates/RR-9001 400 11ms"}
{"_sys":"portal-property","_comp":"gui-gateway","@timestamp":"2026-01-14T10:01:00.000Z","level":"INFO","msg":"Mutation setRoomRate roomRate RR-9001 minStay=0","email":"ops.lead@demo-hotel.test"}
FIXEOF

# ---- log tree: the fixture laid out the way a real archive is ---------------------
# S3S_LAYOUT default {sys}/{comp}/{date}-{hour}* ; odd hours are gzipped to exercise .gz handling.
LOGS="$ROOT/logs"; ARCH="$ROOT/archive"; mkdir -p "$LOGS" "$ARCH/channelrelay-sync-partition-one-log-r1"
python3 - "$FIX" "$LOGS" <<'PY'
import gzip, json, os, sys
fix, root = sys.argv[1:]
files = {}
for l in open(fix):
    if not l.strip(): continue
    o = json.loads(l); ts = o["@timestamp"]
    rel = "%s/%s/%s-%s.log" % (o["_sys"], o["_comp"], ts[:10], ts[11:13])
    files.setdefault(rel, []).append(json.dumps({k: v for k, v in o.items() if not k.startswith("_")}, separators=(",", ":")))
for rel, lines in files.items():
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    data = "\n".join(lines) + "\n"
    if int(rel[-6:-4]) % 2: gzip.open(p + ".gz", "wt").write(data)
    else: open(p, "w").write(data)
# apps that exist in the estate but logged nothing for these bookings (catalog-only)
for app in ("cm-ota2/avail-gateway cm-ota2/booking-job cm-ota2/avail-api cm-ota2/property-api "
            "pms-hub/hub-booking-events pms-hub/avail-api pms-cm-bridge/state-sync-api "
            "pms-cm-bridge/booking-api hub-channel-store/notification-api").split():
    os.makedirs(os.path.join(root, app), exist_ok=True)
PY

# ---- backends: the REAL reference implementations shipped in backends/ -------------
BK=""; for d in "$SCRIPT_DIR/backends" "$HOME/backends" "$HOME/pms_tools/backends"; do [ -f "$d/s3search.py" ] && { BK="$d"; break; }; done
[ -n "$BK" ] || { echo "FAIL: backends/s3search.py not found (keep the repo layout: backends/ next to this script)"; exit 2; }
chmod +x "$BK/s3search.py" "$BK/s3grep.py" 2>/dev/null
export PT_SEARCH_CMD="$BK/s3search.py" PT_GREP_CMD="$BK/s3grep.py"     # never pick up a real backend on PATH
export S3S_ROOT="$LOGS" S3G_ROOT="$ARCH"
echo "== backends: $BK"

# transport shims (no network): 'ssh' runs the remote command locally, 'aws' maps s3:// to $ROOT/s3
SHIM="$ROOT/shim"; mkdir -p "$SHIM" "$ROOT/s3"; ln -s "$LOGS" "$ROOT/s3/demo-logs"
cat > "$SHIM/ssh" <<'SH'
#!/usr/bin/env bash
# test shim: ssh [-n] [-o opt]... target "command"  ->  run command locally
while [ $# -gt 0 ]; do case "$1" in -n) shift;; -o) shift 2;; -*) shift;; *) break;; esac; done
shift; exec bash -c "$*"
SH
cat > "$SHIM/aws" <<PY
#!/usr/bin/env python3
# test shim: aws s3 ls [--recursive] s3://b/p/  |  aws s3 cp s3://b/key -
import os, sys
R = "$ROOT/s3"
a = sys.argv[1:]
def loc(u): return os.path.join(R, u[5:])
if a[:2] == ["s3", "ls"]:
    rec = "--recursive" in a; u = [x for x in a if x.startswith("s3://")][0]; p = loc(u)
    if rec:
        b = u[5:].split("/")[0]
        for d, _, fs in os.walk(os.path.join(R, b), followlinks=True):
            for f in fs:
                key = os.path.relpath(os.path.join(d, f), os.path.join(R, b))
                if (b + "/" + key).startswith(u[5:]): print("2026-01-10 00:00:00 %9d %s" % (1, key))
    elif os.path.isdir(p):
        for n in sorted(os.listdir(p)):
            print("                           PRE %s/" % n if os.path.isdir(os.path.join(p, n)) else "2026-01-10 00:00:00 1 " + n)
    sys.exit(0)
if a[:2] == ["s3", "cp"] and a[3] == "-":
    sys.stdout.buffer.write(open(loc(a[2]), "rb").read()); sys.exit(0)
sys.exit(2)
PY
chmod +x "$SHIM/ssh" "$SHIM/aws"

print_verbose_function_report() {
  local run_dir="$1" call_log="$2"
  echo "==================== VERBOSE FUNCTION REPORT ===================="
  echo "--- [Function: System & App Resolution (resolve.tsv)] ---"
  [ -s "$run_dir/resolve.tsv" ] && cat "$run_dir/resolve.tsv" || echo "(none)"

  echo "--- [Function: Backend Search & Grep Calls (mock_calls.log)] ---"
  [ -s "$call_log" ] && cat "$call_log" || echo "(none)"

  echo "--- [Function: Join Key Harvester (join_keys.tsv)] ---"
  [ -s "$run_dir/join_keys.tsv" ] && cat "$run_dir/join_keys.tsv" || echo "(none)"

  echo "--- [Function: Gate & Failure Verdict Evaluator (verdict.json)] ---"
  [ -s "$run_dir/verdict.json" ] && python3 -m json.tool "$run_dir/verdict.json" || echo "(none)"

  echo "--- [Function: Main Trace Report Generator (REPORT.md)] ---"
  [ -s "$run_dir/REPORT.md" ] && cat "$run_dir/REPORT.md" || echo "(none)"

  echo "--- [Function: Remediation Advisor (NEXT_ACTIONS.md)] ---"
  [ -s "$run_dir/NEXT_ACTIONS.md" ] && cat "$run_dir/NEXT_ACTIONS.md" || echo "(none)"
  echo "================================================================="
}

# ---- scenario runner ------------------------------------------------------------
TOTAL_FAIL=0
run_case() { # name ref at extra-env...
  local name="$1" ref="$2" at="$3"; shift 3
  local H="$ROOT/home_$name"; mkdir -p "$H/pms_tools/kb"
  cp "${KBFROM:-$KBSRC}" "$H/pms_tools/kb/connector_kb.json"
  echo; echo "================ scenario: $name  (ref=$ref at=$at) ================"
  env HOME="$H" PATH="$PATH" S3S_CALL_LOG="$H/mock_calls.log" \
    PT_CASE="mock-$name" PT_REF="$ref" PT_CHANNEL=OTA2 PT_PMS=CLOUDPMS \
    PT_AT="$at" PT_AT_UNCERTAIN=1 PT_FANOUT_HOURS=1 PT_FROM="${at:0:10}" PT_TO="${at:0:10}" \
    PT_TASKS=trace PT_DETACH=0 PT_NO_PREFLIGHT=1 PT_SYS_SWEEP=0 PT_FULLDAY_FALLBACK=1 \
    PT_FOLLOW=1 PT_GREP_FALLBACK=1 PT_MODE=run PT_SEARCH_TIMEOUT=60 PT_CATALOG_TIMEOUT=60 \
    PT_KB_DIR="$H/pms_tools/kb" PT_KB_FILE=connector_kb.json PT_ON_EXISTS=wipe PT_ON_FINISHED=wipe \
    "$@" "$PT_BIN" < /dev/null > "$H/run.log" 2>&1
  local rc=$?; RC=$rc
  RUN="$H/pms_tools/runs/mock-$name"; LOG="$H/run.log"
  CALLS=$(grep -c . "$H/mock_calls.log" 2>/dev/null || echo 0); KBLIVE="$H/pms_tools/kb/connector_kb.json"
  echo "trace exit=$rc  backend calls=$CALLS  kb-write: $(grep -h '\[kb-write\] updated' "$LOG" | tail -1)"
  [ "${VERBOSE:-0}" = "1" ] && print_verbose_function_report "$RUN" "$H/mock_calls.log"
  return 0
}

summ() { python3 - "$RUN" <<'PY'
import json, os, sys, glob
run = sys.argv[1]
v = json.load(open(os.path.join(run, "verdict.json")))
keys = [l.split("\t")[:2] for l in open(os.path.join(run, "join_keys.tsv"))] if os.path.exists(os.path.join(run, "join_keys.tsv")) else []
raw = glob.glob(os.path.join(run, "raw", "*.out"))
nonempty = [f for f in raw if os.path.getsize(f) > 0]
follow = [f for f in nonempty if os.path.basename(f).startswith("f_")]
print("  reached : %s" % v.get("reached"))
print("  missing : %s" % v.get("missing"))
for k in ("failed", "failed_gates", "terminal", "failure_codes", "na"):
    if k in v: print("  %-8s: %s" % (k, v[k]))
print("  join keys (%d): %s" % (len(keys), ", ".join("%s=%s" % (a, b) for a, b in keys)))
print("  evidence files with hits: %d (follow-up hits: %d)" % (len(nonempty), len(follow)))
PY
}
check() { # description python-expression-over(v, keys, run)
  local d="$1" expr="$2"
  if KBP="$KBLIVE" KBSRC_="${KBFROM:-$KBSRC}" LOG_="$LOG" RC_="${RC:-0}" python3 - "$RUN" "$expr" <<'PY'
import json, os, sys, glob
run, expr = sys.argv[1], sys.argv[2]
try: v = json.load(open(os.path.join(run, "verdict.json")))
except Exception: v = {}
jk = os.path.join(run, "join_keys.tsv")
keys = {}
if os.path.exists(jk):
    for l in open(jk):
        p = l.rstrip("\n").split("\t")
        if len(p) >= 2: keys.setdefault(p[0], set()).add(p[1])
nonempty = [os.path.basename(f) for f in glob.glob(os.path.join(run, "raw", "*.out")) if os.path.getsize(f) > 0]
report = open(os.path.join(run, "REPORT.md")).read() if os.path.exists(os.path.join(run, "REPORT.md")) else ""
reached = set(v.get("reached", []))
def txt(rel):  # text of a file in the run dir ("" when absent)
    p = os.path.join(run, rel)
    return open(p, errors="replace").read() if os.path.exists(p) else ""
def js(rel):   # parsed JSON file in the run dir ({} when absent)
    try: return json.load(open(os.path.join(run, rel)))
    except Exception: return {}
log = open(os.environ["LOG_"], errors="replace").read() if os.path.exists(os.environ.get("LOG_", "")) else ""
rc = int(os.environ.get("RC_", "0"))
sys.exit(0 if eval(expr) else 1)
PY
  then echo "  [PASS] $d"; else echo "  [FAIL] $d"; CASE_FAIL=$((CASE_FAIL+1)); fi
}

# ---- scenario 1: healthy booking, end-to-end delivered ----------------------------
run_case delivered OTA2-1000000001 2026-01-10T12:00:00
CASE_FAIL=0
if [ -s "$RUN/verdict.json" ]; then summ; else echo "  verdict.json missing - log tail:"; tail -n 25 "$LOG"; fi
check "verdict.json written"                                  "bool(v)"
check "gates ARRIVAL,ACCEPTANCE,CM_STORE,PMS_RECEIVE,DELIVERY lit" "{'ARRIVAL','ACCEPTANCE','CM_STORE','PMS_RECEIVE','DELIVERY'} <= reached"
check "CONVERSION not lit (no failure)"                       "'CONVERSION' not in reached and not v.get('failure_codes')"
check "flowToken harvested"                                  "'0a1b2c3d-1111-4222-8333-944455556666' in keys.get('flowToken', set())"
check "hubBookingUuid harvested"                           "'9c8b7a65-4321-4fed-8cba-0123456789ab' in keys.get('hubBookingUuid', set())"
check "follow-up sweeps produced hits (f_*.out)"              "any(f.startswith('f_') for f in nonempty)"
check "co-tenant booking OTA2-1000000777 NOT in evidence"     "'ffffffff-0000-4000-8000-000000000777' not in str(keys)"
check "REPORT.md + NEXT_ACTIONS.md written"                   "bool(report) and os.path.exists(os.path.join(run,'NEXT_ACTIONS.md'))"
KB1="$KBLIVE"; CALLS1=$CALLS
check "KB write-back: timestamped .bak created"               "len(glob.glob(os.environ['KBP'] + '.bak.*')) == 1"
check "KB write-back: run recorded in channels.OTA2.history"  "json.load(open(os.environ['KBP']))['channels']['OTA2']['history'][-1]['case'] == 'mock-delivered'"
check "KB write-back: pms delivery_apps NOT polluted by channel apps" "not [a for a in json.load(open(os.environ['KBP']))['pms']['CLOUDPMS']['delivery_apps'] if not a.startswith('pms-')]"
check "KB write-back: KB-declared apps kept (no false stale repair)" "json.load(open(os.environ['KBP']))['channels']['OTA2']['log_apps'].get('ari_outbound') == json.load(open(os.environ['KBSRC_']))['channels']['OTA2']['log_apps'].get('ari_outbound')"
[ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 20 "$LOG"; }

# ---- scenario 1b: second session starts from the KB the first session wrote ----------
KBFROM="$KB1" run_case rerun OTA2-1000000001 2026-01-10T12:00:00
CASE_FAIL=0
[ -s "$RUN/verdict.json" ] && summ
check "same gates reached from the written-back KB"           "{'ARRIVAL','ACCEPTANCE','CM_STORE','PMS_RECEIVE','DELIVERY'} <= reached"
check "loaded the updated KB (history now has 2 runs)"        "len(json.load(open(os.environ['KBP']))['channels']['OTA2']['history']) == 2"
check "KB converged: second session learned no new apps"     "'channels.OTA2 +0 apps' in log"
echo "  note: backend calls $CALLS1 -> $CALLS (session 1 learned cm-ota2/notify-gateway from an echoToken follow-up hit; session 2 also scans it)"
[ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 20 "$LOG"; }

# ---- scenario 3: discovery + self-learning (channel module has NO app paths) -----------
# Session A must discover the channel system from the catalog, sweep the verified pool and write
# the apps that produced hits back to the KB; session B must start from those apps and need fewer calls.
KBBLANK="$ROOT/kb_blank_ota2.json"
python3 - "$KBSRC" "$KBBLANK" <<'PY'
import json, sys
k = json.load(open(sys.argv[1])); k["channels"]["OTA2"]["log_apps"] = {"reservation_inbound": None, "ari_outbound": None}
json.dump(k, open(sys.argv[2], "w"), indent=1)
PY
KBFROM="$KBBLANK" run_case discover OTA2-1000000001 2026-01-10T12:00:00
CASE_FAIL=0
[ -s "$RUN/verdict.json" ] && summ
KBD="$KBLIVE"; CALLSD=$CALLS
check "system discovered via cm-<prefix> convention (pool-verified)" "'sys_from_convention|OTA2|cm-ota2' in open(os.path.join(run,'resolve.tsv')).read()"
check "gates reached despite empty KB module"                 "{'ARRIVAL','ACCEPTANCE','CM_STORE','PMS_RECEIVE','DELIVERY'} <= reached"
check "KB learned channels.OTA2.log_apps.reservation_inbound" "json.load(open(os.environ['KBP']))['channels']['OTA2']['log_apps'].get('reservation_inbound') == 'cm-ota2/booking-gateway'"
[ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 20 "$LOG"; }
KBFROM="$KBD" run_case relearn OTA2-1000000001 2026-01-10T12:00:00
CASE_FAIL=0
[ -s "$RUN/verdict.json" ] && summ
check "session B resolved from learned KB apps (authoritative)" "'sys_from_kb_apps|OTA2|cm-ota2' in open(os.path.join(run,'resolve.tsv')).read()"
check "session B same gates"                                  "{'ARRIVAL','ACCEPTANCE','CM_STORE','PMS_RECEIVE','DELIVERY'} <= reached"
check "session B cheaper: $CALLS calls < $CALLSD (discovery)"  "$CALLS < $CALLSD"
[ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 20 "$LOG"; }

# ---- scenario 4: booking arrives but PMS conversion fails (terminal signature) -----
run_case convfail OTA2-2000000002 2026-01-10T15:00:00
CASE_FAIL=0
if [ -s "$RUN/verdict.json" ]; then summ; else echo "  verdict.json missing - log tail:"; tail -n 25 "$LOG"; fi
check "verdict.json written"                                  "bool(v)"
check "ARRIVAL, ACCEPTANCE, CM_STORE lit"                     "{'ARRIVAL','ACCEPTANCE','CM_STORE'} <= reached"
check "failure code ERR_CONVERSION_UNMAPPED_ROOM_RATE captured" "'ERR_CONVERSION_UNMAPPED_ROOM_RATE' in v.get('failure_codes', {})"
check "DELIVERY not reported as success"                      "'DELIVERY' not in reached"
check "deeper key hubBookingUuid harvested from follow hits" "'7d7d7d7d-aaaa-4bbb-8ccc-dddddddddddd' in keys.get('hubBookingUuid', set())"
check "terminal signature recognised from KB"                 "v.get('terminal') == 'ERR_CONVERSION_UNMAPPED_ROOM_RATE'"
[ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 20 "$LOG"; }

# ---- scenario 5: backend contract (direct calls) -----------------------------------
echo; echo "================ scenario: backend contract (backends/s3search.py, s3grep.py) ================"
CASE_FAIL=0
bk() { python3 "$BK/s3search.py" "$@"; }
expect() { # label expected-rc command...
  local label="$1" want="$2"; shift 2; local out rc; out=$("$@" 2>&1); rc=$?
  if [ "$rc" = "$want" ]; then
    echo "  [PASS] $label (rc=$rc, $(printf '%s' "$out" | grep -c .) lines)"
    [ "${VERBOSE:-0}" = "1" ] && [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/         | /'
  else
    echo "  [FAIL] $label (rc=$rc want $want): $(printf '%s' "$out" | head -2)"; CASE_FAIL=$((CASE_FAIL+1))
  fi
}

expect "ls * * lists the estate"                     0 bk ls '*' '*'
expect "ls glob cm-ota2 booking-*"               0 bk ls cm-ota2 'booking-*'
expect "grep one hour (plain file)"                  0 bk grep cm-ota2 booking-gateway 2026-01-10-12 OTA2-1000000001
expect "grep one hour (.gz file)"                    0 bk grep cm-ota2 booking-gateway 2026-01-10-15 OTA2-2000000002
expect "grep whole day, sys glob"                    0 bk grep '*' '*' '2026-01-10-*' 9c8b7a65-4321-4fed-8cba-0123456789ab
expect "grep wrong hour -> no match"                 1 bk grep cm-ota2 booking-gateway 2026-01-10-09 OTA2-1000000001
expect "query field equality"                        0 bk query pms-hub '*' '2026-01-10-*' 's.faultCode = "ERR_CONVERSION_UNMAPPED_ROOM_RATE"'
expect "query AND + LIKE"                            0 bk query cm-ota2 '*' '2026-01-10-*' "s.reservationId LIKE '%1000000001' AND s.statusCode = '200'"
expect "query non-matching field value"              1 bk query cm-ota2 '*' '2026-01-10-*' 's.reservationId = "OTA2-1000000"'
expect "unsafe name rejected"                        2 bk ls 'cm;rm -rf' '*'
expect "bad blob rejected"                           2 bk grep cm-ota2 '*' 2026-01-10 x
expect "s3grep find (empty legacy archive)"          1 python3 "$BK/s3grep.py" find OTA2-1 'channelrelay*partition*one*' 2026-01-10
L=$(bk grep '*' '*' '2026-01-10-*' OTA2-1000000001 | sort | cksum)
S=$(PATH="$SHIM:$PATH" S3S_ROOT="ssh://ops@loghost$LOGS" bk grep '*' '*' '2026-01-10-*' OTA2-1000000001 | sort | cksum)
A=$(PATH="$SHIM:$PATH" S3S_ROOT="s3://demo-logs" bk grep '*' '*' '2026-01-10-*' OTA2-1000000001 | sort | cksum)
[ "$L" = "$S" ] && echo "  [PASS] ssh:// transport returns identical lines" || { echo "  [FAIL] ssh:// transport differs"; CASE_FAIL=$((CASE_FAIL+1)); }
[ "$L" = "$A" ] && echo "  [PASS] s3:// transport returns identical lines"  || { echo "  [FAIL] s3:// transport differs";  CASE_FAIL=$((CASE_FAIL+1)); }
TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL))

# ---- scenario 6/7: full trace over ssh:// and s3:// ---------------------------------
for T in "ssh:ssh://ops@loghost$LOGS" "s3:s3://demo-logs"; do
  PATH="$SHIM:$PATH" S3S_ROOT="${T#*:}" run_case "via-${T%%:*}" OTA2-1000000001 2026-01-10T12:00:00
  CASE_FAIL=0
  [ -s "$RUN/verdict.json" ] && summ
  check "all five gates lit over ${T%%:*} transport" "{'ARRIVAL','ACCEPTANCE','CM_STORE','PMS_RECEIVE','DELIVERY'} <= reached"
  [ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 20 "$LOG"; }
done

# =====================================================================================
# MODE COVERAGE: every task the dispatcher knows (fidelity, ari, guiattrib, kb-test,
# bridgeflush) plus goal playbooks - each run against its own fixture slice above.
# =====================================================================================
print_mode_report() { # run_dir call_log - verbose: artefacts a mode wrote
  local run_dir="$1" call_log="$2" f
  echo "==================== VERBOSE MODE REPORT ===================="
  echo "--- [Backend calls (mock_calls.log)] ---"; [ -s "$call_log" ] && cat "$call_log" || echo "(none)"
  for f in verdict.json ari_evidence.tsv ari_actors.txt fidelity_report.txt gui_tokens.txt GUIACTORS.md GOAL_VERDICT.md state/bfl_analysis.json REPORT.md NEXT_ACTIONS.md; do
    [ -s "$run_dir/$f" ] && { echo "--- [$f] ---"; cat "$run_dir/$f"; }
  done
  echo "--- [run log] ---"; cat "$LOG"
  echo "============================================================="
}
run_mode() { # name tasks extra-env...   (no trace defaults: each mode gets only its own inputs)
  local name="$1" tasks="$2"; shift 2
  local H="$ROOT/home_$name"; mkdir -p "$H/pms_tools/kb"
  cp "$KBSRC" "$H/pms_tools/kb/connector_kb.json"
  echo; echo "================ scenario: $name  (PT_TASKS='$tasks') ================"
  env HOME="$H" PATH="$PATH" S3S_CALL_LOG="$H/mock_calls.log" \
    PT_CASE="mock-$name" PT_TASKS="$tasks" PT_DETACH=0 PT_NO_PREFLIGHT=1 PT_SYS_SWEEP=0 PT_MODE=run \
    PT_SEARCH_TIMEOUT=60 PT_CATALOG_TIMEOUT=60 PT_KB_DIR="$H/pms_tools/kb" PT_KB_FILE=connector_kb.json \
    PT_ON_EXISTS=wipe PT_ON_FINISHED=wipe \
    "$@" "$PT_BIN" < /dev/null > "$H/run.log" 2>&1
  RC=$?
  RUN="$H/pms_tools/runs/mock-$name"; LOG="$H/run.log"; KBLIVE="$H/pms_tools/kb/connector_kb.json"
  CALLS=$(grep -c . "$H/mock_calls.log" 2>/dev/null || echo 0)
  echo "exit=$RC  backend calls=$CALLS"
  [ "${VERBOSE:-0}" = "1" ] && print_mode_report "$RUN" "$H/mock_calls.log"
  CASE_FAIL=0
}
case_end() { [ $CASE_FAIL -gt 0 ] && { TOTAL_FAIL=$((TOTAL_FAIL+CASE_FAIL)); echo "  log tail:"; tail -n 25 "$LOG"; }; true; }
NO_TB="'Traceback' not in log"

# ---- fidelity (a): offline documents given as files -> every field PRESERVED -----------
DOCS="$ROOT/docs"; mkdir -p "$DOCS"
cat > "$DOCS/ota.xml" <<'XML'
<OTA_HotelResNotifRQ EchoToken="ECHO-0000"><RoomType RoomTypeCode="DBL"/><RatePlan RatePlanCode="BAR"/><Total AmountAfterTax="220.00" CurrencyCode="EUR"><Taxes><Tax Amount="20.00" Code="VAT" Type="Inclusive"/></Taxes></Total><TimeSpan Start="2026-02-14" End="2026-02-16"/><GivenName>Alex</GivenName></OTA_HotelResNotifRQ>
XML
cat > "$DOCS/pms.xml" <<'XML'
<Reservation><valueNum>220.00</valueNum><Total CurrencyCode="EUR"><Taxes><Tax Amount="20.00" Code="VAT" Type="Inclusive"/></Taxes></Total><RoomType RoomTypeCode="DBL"/><RatePlan RatePlanCode="BAR"/><TimeSpan Start="2026-02-14" End="2026-02-16"/><GivenName>Alex</GivenName></Reservation>
XML
run_mode fidelity-files fidelity PT_OTA_FILE="$DOCS/ota.xml" PT_PMS_FILE="$DOCS/pms.xml"
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "fidelity_report.txt written"                        "txt('fidelity_report.txt').startswith('FIDELITY REPORT')"
check "amount/currency/room/rate/guest/dates/taxes PRESERVED" "txt('fidelity_report.txt').count('PRESERVED') == 7"
check "no verdicts raised for identical documents"         "txt('fidelity_report.txt').rstrip().endswith('VERDICTS:')"
case_end

# ---- fidelity (b): no documents and nothing to fetch -> honest 'unavailable' ---------------
run_mode fidelity-nodocs fidelity
check "reports docs unavailable instead of guessing"       "'[fidelity] docs unavailable' in log and not txt('fidelity_report.txt')"
case_end

# ---- trace + fidelity chained: docs auto-fetched via harvested echoToken / hubBookingUuid ---
run_mode trace-fidelity "trace fidelity" PT_REF=OTA2-1000000001 PT_CHANNEL=OTA2 PT_PMS=CLOUDPMS \
  PT_AT=2026-01-10T12:00:00 PT_AT_UNCERTAIN=1 PT_FANOUT_HOURS=1 PT_FROM=2026-01-10 PT_TO=2026-01-10 \
  PT_FULLDAY_FALLBACK=1 PT_FOLLOW=1 PT_GREP_FALLBACK=1
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "echoToken harvested by trace"                       "'ECHO-7f3a9c21' in txt('join_keys.tsv')"
check "OTA doc auto-fetched (notify-gateway by echoToken)" "'OTA_HotelResNotifRQ' in txt('raw/ota_doc.xml')"
check "PMS doc auto-fetched (booking-api by hubBookingUuid)" "'<Reservation>' in txt('raw/pms_doc.xml')"
check "CONFIG_IGNORED verdict (PMS got after-tax amount)"  "'CONFIG_IGNORED' in txt('fidelity_report.txt')"
check "TAX_BREAKDOWN_DROPPED verdict"                      "'TAX_BREAKDOWN_DROPPED: 1 tax lines' in txt('fidelity_report.txt')"
check "before-tax amount reported DROPPED"                 "'FIELD_DROPPED: rate.amount_before_tax' in txt('fidelity_report.txt')"
case_end

# ---- ari (a) via goal playbook rate-mismatch: manual GUI change, attributed to a user -------
run_mode ari-manual ari PT_GOAL=rate-mismatch PT_CHANNEL=OTA2 PT_HOTEL_UUID=33333333-4444-4555-8666-777777777777 \
  PT_MEMBERSHIP=LA-778899 PT_TARGET_VALUES=129.00 PT_STAY_DATES=2026-02-14 \
  PT_AT=2026-01-12T10:00:00 PT_FROM=2026-01-11 PT_TO=2026-01-12 PT_REGION=both PT_SOURCE_SYSTEM=auto
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "goal input contract satisfied"                      "'all required anchors present' in log"
check "target-hour sweep stopped at 2026-01-12-09"         "'TARGET hour found at 2026-01-12-09' in log"
check "SET line classified TARGET_EVENT (value+date)"      "'TARGET_EVENT' in txt('ari_evidence.tsv') and v.get('value_events') == 1"
check "actor resolved via gui-gateway flowToken"           "txt('ari_actors.txt').strip() == 'jane.doe@demo-hotel.test'"
check "gates SOURCE,CM_PERSIST,CHANNEL_PUSH,OTA_ACK lit"   "{'SOURCE','CM_PERSIST','CHANNEL_PUSH','OTA_ACK'} <= reached"
check "INGEST is N/A for a manual origin"                  "v.get('na') == ['INGEST']"
check "GOAL_VERDICT = VALUE_ATTRIBUTED"                    "'VALUE_ATTRIBUTED' in txt('GOAL_VERDICT.md')"
check "NEXT_ACTIONS names the user"                        "'Manual change confirmed' in txt('NEXT_ACTIONS.md')"
check "KB write-back: push-leg app confirmed"              "'cm-ota2/avail' in json.dumps(json.load(open(os.environ['KBP']))['channels']['OTA2'])"
case_end

# ---- ari (b): PMS push origin -> ingest leg on the bridge, region rb, no channel -----------
run_mode ari-pms ari PT_HOTEL_UUID=44444444-5555-4666-8777-888888888888 PT_PROPERTY_ID=PK-445566 \
  PT_TARGET_VALUES=3 PT_STAY_DATES=2026-02-20 PT_AT=2026-01-12T18:00:00 PT_FROM=2026-01-12 PT_TO=2026-01-12 \
  PT_REGION=both PT_SOURCE_SYSTEM=auto
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "SET found in region rb after ra came back empty"    "'avail-api-rb' in txt('ari_evidence.tsv') and 'avail-api-ra' not in txt('ari_evidence.tsv')"
check "origin CLOUDPMS -> ingest leg auto-selected (bridge)" "'CloudPMS ingest leg' in log"
check "gates SOURCE,INGEST,CM_PERSIST lit"                 "{'SOURCE','INGEST','CM_PERSIST'} <= reached"
check "CHANNEL_PUSH honestly missing (no channel given)"   "'CHANNEL_PUSH' in v.get('missing', [])"
check "NEXT_ACTIONS points at the PMS send log"            "'PMS push origin' in txt('NEXT_ACTIONS.md')"
case_end

# ---- goal input contract: missing anchors -> refuse before any scan ------------------------
run_mode goal-contract ari PT_GOAL=overbooking PT_CHANNEL=OTA2
check "refuses with exit 2"                                "rc == 2 and 'REFUSING to run with gaps' in log"
check "lists the missing anchors"                          "'PT_HOTEL_UUID' in log and 'PT_STAY_DATES' in log"
check "no backend call was made"                           "$CALLS == 0"
case_end

# ---- guiattrib: GUI config change -> token -> actor email/IP + audit write ---------------
run_mode guiattrib guiattrib PT_GUI_DATE=2026-01-13 PT_GUI_HOUR=14 PT_GUI_USER=sam.ops@demo-hotel.test \
  PT_CHANNEL_HOTEL_UUID=55555555-6666-4777-8888-999999999999 PT_PROPERTY_ID=PK-445566 PT_MEMBERSHIP=LA-778899 PT_REGION=ra
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "flowToken extracted from the link-api burst"        "'6b6b6b6b-1111-4222-8333-944455550001' in txt('gui_tokens.txt')"
check "actor email + client IP in GUIACTORS.md"            "'sam.ops@demo-hotel.test' in txt('GUIACTORS.md') and '198.51.100.23' in txt('GUIACTORS.md')"
check "mutation setChannelProperty attributed"             "'Mutation setChannelProperty' in txt('GUIACTORS.md')"
check "platform session read captured"                     "'PK-445566' in txt('raw/gui_session.out')"
check "entity-api audit write matched"                     "'Rate modification applied' in txt('GUIACTORS.md')"
check "summary counts 1 token / 1 mutation / 1 audit hit"  "'1 tokens, 1 user mutations, 1 core audit hits' in log"
case_end

# ---- kb-test: KB modules cross-checked against the live catalog --------------------------
run_mode kb-test kb-test
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "every KB module reported"                           "all(('[kb-test] %s %s:' % (s, c)) in log for s, cs in (('channel', json.load(open(os.environ['KBP']))['channels']), ('pm', json.load(open(os.environ['KBP']))['pms'])) for c in cs)"
check "fixture app reported LIVE"                          "'[kb-test] LIVE  cm-ota2/booking-gateway' in log"
check "app absent from the archive reported STALE?"        "'[kb-test] STALE?  xengine' in log"
check "failure signatures counted"                         "'failure_signatures: %d loaded' % len(json.load(open(os.environ['KBP']))['failure_signatures']) in log"
case_end

# ---- bridgeflush via goal playbook bridge-flush: culprit room rate DERIVED from logs ---------
run_mode bridgeflush bridgeflush PT_GOAL=bridge-flush PT_PROPERTY_ID=PK-445566 PT_CLOUDPMS_PROPERTY_ID=880011 \
  PT_AT=2026-01-14T11:00:00 PT_FROM=2026-01-13 PT_TO=2026-01-14 PT_REGION=ra
check "exit 0, no python traceback"                        "rc == 0 and $NO_TB"
check "flush trigger found at 2026-01-14-10"               "'2026-01-14-10' in txt('state/t_bfl_trig_hours.txt')"
check "failing sub-leg isolated: hub-pms-sync"             "js('state/bfl_analysis.json').get('failing_subleg') == 'hub-pms-sync'"
check "culprit room rate derived: RT-DBL/RR-9001"          "js('state/bfl_analysis.json').get('culprit_ids') == ['RT-DBL/RR-9001']"
check "token chained cloudpms/main <-> hub-sync-api"       "js('state/bfl_analysis.json').get('token_chain') == ['7a7a7a7a-1111-4222-8333-944455550002']"
check "all 8 bridge-flush gates lit"                       "report.count('- [x] ') == 8 and '- [ ] ' not in report"
check "fail-fast single-blocker pattern confirmed"         "'Fail-fast pattern CONFIRMED: 2 distinct room rates' in report"
check "actor + origin captured"                            "'user=ops.lead@demo-hotel.test' in report and 'ORIGIN: 1 platform-side hit' in report"
case_end

# ---- unknown task name: reported, not silently ignored ---------------------------------
# PT_TASKS is operator-typed free text; a typo must be named, and the valid tasks still run.
run_mode unknown-task "not-a-real-task kb-test"
check "unknown task named with the valid list"             "\"unknown task 'not-a-real-task' (trace|fidelity|kb-test|ari|guiattrib|bridgeflush)\" in log"
check "remaining valid task still ran, exit 0"             "'[kb-test] LIVE  cm-ota2/booking-gateway' in log and rc == 0"
case_end

echo
if [ $TOTAL_FAIL -eq 0 ]; then echo "RESULT: PASS (all scenarios, all modes)"; exit 0; else echo "RESULT: FAIL ($TOTAL_FAIL check(s) failed)"; exit 1; fi
