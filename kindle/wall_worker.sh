#!/bin/sh
# generated: true
# Kindle Paperwhite 3 (5.16.2.1.1) always-on weather wall worker.
# Verified primitives on PW3: stop framework, lipc powerd, /usr/sbin/eips -f -g,
# /sys/class/rtc/rtc0/wakealarm + echo mem > /sys/power/state, /usr/bin/curl.

trap '' HUP
export PATH="/usr/sbin:/sbin:/usr/bin:/bin"

BASE="/mnt/us/weather-wall"
CONFIG="$BASE/config"
CACHE="$BASE/cache"
LOG="$BASE/wall.log"
PIDFILE="$BASE/wall.pid"
EIPS="/usr/sbin/eips"
RTC="/sys/class/rtc/rtc0/wakealarm"

WALL_URL=""
WALL_TOKEN=""
REFRESH_MINUTES="60"
RETRY_MINUTES="15"
FRONTLIGHT="0"
LOG_LINES="400"

mkdir -p "$CACHE"

log() {
    printf '%s (epoch=%s) %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(date +%s)" "$*" >> "$LOG"
    if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt "$LOG_LINES" ]; then
        tail -n "$LOG_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
    sync
}

[ -f "$CONFIG" ] && . "$CONFIG"

case "$REFRESH_MINUTES" in ''|*[!0-9]*|0*) REFRESH_MINUTES="60" ;; esac
case "$RETRY_MINUTES" in ''|*[!0-9]*|0*) RETRY_MINUTES="15" ;; esac
case "$FRONTLIGHT" in ''|*[!0-9]*) FRONTLIGHT="0" ;; esac

if [ -f "$PIDFILE" ]; then
    OLD_PID="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$OLD_PID" ] && [ "$OLD_PID" != "$$" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
fi
printf '%s\n' "$$" > "$PIDFILE"

wait_for_wifi() {
    max_s="$1"
    i=0
    while [ "$i" -lt "$max_s" ]; do
        st="$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null)"
        if [ "$st" = "CONNECTED" ]; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    log "wifi not CONNECTED after ${max_s}s (state=$st)"
    return 1
}

is_valid_png() {
    [ -s "$1" ] || return 1
    magic="$(dd if="$1" bs=4 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    [ "$magic" = "89504e47" ]
}

fetch_frame() {
    [ -n "$WALL_URL" ] || { log "ERROR: WALL_URL not configured"; return 1; }
    rm -f "$CACHE/next.png"
    if [ -n "$WALL_TOKEN" ]; then
        /usr/bin/curl -sfL --connect-timeout 15 -m 25 \
            -H "X-Wall-Token: $WALL_TOKEN" \
            -o "$CACHE/next.png" "$WALL_URL" 2>>"$LOG"
    else
        /usr/bin/curl -sfL --connect-timeout 15 -m 25 \
            -o "$CACHE/next.png" "$WALL_URL" 2>>"$LOG"
    fi
    if is_valid_png "$CACHE/next.png"; then
        mv "$CACHE/next.png" "$CACHE/current.png"
        sync
        return 0
    fi
    rm -f "$CACHE/next.png"
    log "fetch failed or invalid PNG from $WALL_URL"
    return 1
}

paint_crisp() {
    [ -s "$CACHE/current.png" ] || { log "paint: no cached frame"; return 1; }
    # Wait 3s after full paint so EPDC finishes driving ink particles before deep sleep
    "$EIPS" -c >/dev/null 2>&1
    sleep 1
    "$EIPS" -f -g "$CACHE/current.png" >/dev/null 2>&1 || "$EIPS" -g "$CACHE/current.png" >/dev/null 2>&1
    sleep 3
    sync
    return 0
}

suspend_until() {
    target="$1"
    now="$(date +%s)"
    delta=$((target - now))
    if [ "$delta" -lt 10 ]; then
        sleep 10
        return 0
    fi
    batt="$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)"
    log "suspend for ${delta}s -> $target (batt=${batt}%)"
    echo 0 > "$RTC" 2>>"$LOG"
    if ! echo "$target" > "$RTC" 2>>"$LOG"; then
        log "ERROR: wakealarm write failed; falling back to sleep"
        sleep "$delta"
        return 0
    fi
    sync
    if ! echo mem > /sys/power/state 2>>"$LOG"; then
        log "ERROR: echo mem failed; falling back to sleep"
        sleep "$delta"
        return 0
    fi
    batt2="$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)"
    log "resumed (batt=${batt2}%)"
    sleep 3
    return 0
}

log "=== WeatherWall worker start (pid=$$, refresh=${REFRESH_MINUTES}m, retry=${RETRY_MINUTES}m) ==="
sleep 3

lipc-set-prop com.lab126.powerd preventScreenSaver 1 >/dev/null 2>&1
lipc-set-prop com.lab126.powerd flIntensity "$FRONTLIGHT" >/dev/null 2>&1
stop framework >/dev/null 2>&1 || true
log "framework stopped"
sleep 2

PAINTED_ONCE="0"

while :; do
    GOT_FRESH="0"
    if wait_for_wifi 30; then
        if fetch_frame; then
            GOT_FRESH="1"
        fi
    fi

    if [ "$GOT_FRESH" = "1" ]; then
        paint_crisp
        PAINTED_ONCE="1"
        batt="$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)"
        log "refresh: fresh (batt=${batt}%)"
        period=$((REFRESH_MINUTES * 60))
        now="$(date +%s)"
        if [ "$REFRESH_MINUTES" -ge 5 ]; then
            next_wake=$(( (now / period + 1) * period ))
            if [ $((next_wake - now)) -lt 20 ]; then
                next_wake=$((next_wake + period))
            fi
        else
            next_wake=$((now + period))
        fi
    else
        if [ "$PAINTED_ONCE" = "0" ] && [ -s "$CACHE/current.png" ]; then
            paint_crisp
            PAINTED_ONCE="1"
            log "refresh: offline startup, painted cached frame"
        else
            log "refresh: offline, keeping current screen untouched"
        fi
        now="$(date +%s)"
        next_wake=$((now + RETRY_MINUTES * 60))
    fi

    suspend_until "$next_wake"
done
