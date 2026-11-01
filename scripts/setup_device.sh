#!/usr/bin/env bash
# One-time phone setup. Run from a host with the phone plugged in via USB
# (USB debugging enabled in Developer options).
set -euo pipefail

PHONE_IP="${1:?usage: setup_device.sh <phone-lan-ip>}"

echo "== enabling adb over tcpip (needs USB once per reboot)"
adb tcpip 5555
sleep 2
adb connect "$PHONE_IP:5555"

SERIAL="$PHONE_IP:5555"

echo "== granting mock-location provider rights to Lexa Fake GPS"
adb -s "$SERIAL" shell appops set com.lexa.fakegps android:mock_location allow
# belt & suspenders: also set the developer-option selector
adb -s "$SERIAL" shell settings put secure mock_location 1 || true

echo "== keep screen responsive / kill doze for our apps"
adb -s "$SERIAL" shell dumpsys deviceidle whitelist +com.lexa.fakegps
adb -s "$SERIAL" shell dumpsys deviceidle whitelist +by.allsports.holder

echo "== verifying"
adb -s "$SERIAL" shell getprop sys.boot_completed
adb -s "$SERIAL" shell pm list packages | grep -E "lexa.fakegps|allsports.holder"
echo "done"
