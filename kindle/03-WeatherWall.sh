#!/bin/sh
# generated: true
# Name: 03-WeatherWall
# Author: KindleWall
# DontUseFBInk

export PATH="/usr/sbin:/sbin:/usr/bin:/bin"
BASE="/mnt/us/weather-wall"
mkdir -p "$BASE"

/usr/sbin/eips 2 38 "Starting WeatherWall $(date '+%H:%M:%S')..." >/dev/null 2>&1
setsid /bin/sh "$BASE/wall_worker.sh" >> "$BASE/wall.log" 2>&1 </dev/null &
sleep 2
sync
