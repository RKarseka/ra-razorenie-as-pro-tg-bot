# Duty phone setup

Works on any of: Samsung A26 (Android 15) or Redmi Note 7 (Android 9/10 MIUI).

## Android settings

- Developer options: ON → **USB debugging**, (Android 11+) **Wireless debugging**.
- Apps: **Fake GPS (com.lexa.fakegps)** and **Allsports (by.allsports.holder)** installed.
- Developer options → **Select mock location app** → Fake GPS.
  (the setup script also grants it via appops)
- Battery: disable optimization for Fake GPS, Allsports.
  - MIUI (Redmi): Settings → Apps → per-app → Battery saver → *No restrictions*,
    plus Autostart ON for both apps. MIUI kills background services otherwise.
- Wi-Fi: fixed channel, "keep Wi-Fi on during sleep" = always.
- Charge: phone lives on the charger; screen timeout can stay short.
- Lock screen: use no PIN, password, or pattern. The bot wakes the screen and
  dismisses an unsecured keyguard before every check-in.

## First USB session

```bash
# phone on USB, then:
adb tcpip 5555
./scripts/setup_device.sh 192.168.1.50
```

After that USB is only needed again if the phone reboots.

## Allsports login

Log into the Allsports account once, manually. The bot never touches credentials.

## Verify

```bash
adb connect 192.168.1.50:5555
adb shell am start-foreground-service -a com.lexa.fakegps.START \
  -n com.lexa.fakegps/.FakeGPSService --es lat 53.9045 --es long 27.5615
# open Google Maps / Allsports on the phone -> position should be Minsk centre
adb shell am start-foreground-service -a com.lexa.fakegps.STOP \
  -n com.lexa.fakegps/.FakeGPSService
```

## Calibration

The check-in UI flow is text-selector driven and may need one calibration pass
on the real device after login: `/shot` in the bot returns a screenshot;
`uiautomator dump` shows element texts/ids used in `bot/device.py::checkin`.
