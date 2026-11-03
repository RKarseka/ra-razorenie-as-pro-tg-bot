"""Device control over network ADB: Lexa Fake GPS intents + UI automation in Allsports.

Verified against com.lexa.fakegps 2.1.2 (decompiled):
  FakeGPSService handles intents com.lexa.fakegps.START / com.lexa.fakegps.STOP.
  START extras: "lat" and "long" as string extras, optional bool "no_history".
"""

from __future__ import annotations

import io
import logging
import math
import random
import re
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

ADB = "adb"
FAKEGPS_START = "com.lexa.fakegps.START"
FAKEGPS_STOP = "com.lexa.fakegps.STOP"
FAKEGPS_SERVICE = "com.lexa.fakegps/.FakeGPSService"
EARTH_RADIUS_M = 6_371_000
LOCATION_JITTER_MIN_M = 10
LOCATION_JITTER_MAX_M = 30


class DeviceError(RuntimeError):
    pass


@dataclass
class Step:
    name: str
    screenshot: bytes | None = None


def jitter_location(lat: float, lon: float) -> tuple[float, float, float]:
    distance_m = random.uniform(LOCATION_JITTER_MIN_M, LOCATION_JITTER_MAX_M)
    bearing = random.uniform(0, 2 * math.pi)
    angular_distance = distance_m / EARTH_RADIUS_M
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    jittered_lat = math.asin(
        math.sin(lat_rad) * math.cos(angular_distance)
        + math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing)
    )
    jittered_lon = lon_rad + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat_rad),
        math.cos(angular_distance) - math.sin(lat_rad) * math.sin(jittered_lat),
    )

    return math.degrees(jittered_lat), math.degrees(jittered_lon), distance_m


class Device:
    def __init__(self, serial: str, allsports_pkg: str = "by.allsports.holder"):
        self.serial = serial
        self.allsports_pkg = allsports_pkg

    # ---------- raw adb ----------

    def _adb(self, *args: str, timeout: int = 30) -> str:
        cmd = [ADB, "-s", self.serial, *args]
        log.info("adb %s", " ".join(args))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise DeviceError(f"adb {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def connect(self) -> str:
        r = subprocess.run(
            [ADB, "connect", self.serial], capture_output=True, text=True, timeout=15
        )
        out = (r.stdout + r.stderr).strip()
        if "connected" not in out:
            raise DeviceError(f"adb connect: {out}")
        return out

    # ---------- fake gps ----------

    def set_location(self, lat: float, lon: float) -> None:
        actual_lat, actual_lon, offset_m = jitter_location(lat, lon)
        log.info(
            "location jitter: %.1fm; requested=%.7f,%.7f actual=%.7f,%.7f",
            offset_m,
            lat,
            lon,
            actual_lat,
            actual_lon,
        )
        self._adb(
            "shell", "am", "start-foreground-service",
            "-a", FAKEGPS_START,
            "-n", FAKEGPS_SERVICE,
            "--es", "lat", str(actual_lat),
            "--es", "long", str(actual_lon),
        )

    def clear_location(self) -> None:
        self._adb(
            "shell", "am", "start-foreground-service",
            "-a", FAKEGPS_STOP,
            "-n", FAKEGPS_SERVICE,
        )

    # ---------- apps ----------

    def wake_and_unlock(self) -> None:
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        self._adb("shell", "wm", "dismiss-keyguard")

    def open_allsports(self) -> None:
        self._adb(
            "shell", "monkey", "-p", self.allsports_pkg,
            "-c", "android.intent.category.LAUNCHER", "1",
        )

    def screenshot(self) -> bytes:
        r = subprocess.run(
            [ADB, "-s", self.serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout:
            raise DeviceError(f"screencap failed: {r.stderr.decode(errors='replace')}")
        return r.stdout

    def current_app(self) -> str:
        return self._adb(
            "shell", "dumpsys", "window"
        ).split("mCurrentFocus=")[-1].splitlines()[0]

    # ---------- check-in flow ----------

    def checkin(self, place_name: str, lat: float, lon: float,
                on_step=None) -> list[Step]:
        """Drive the Allsports check-in. Returns per-step screenshots.

        on_step(name) is called after each stage for progress reporting.
        Selectors are text-based; calibrate via /shot + uiautomator dump on
        the real device if a stage fails.
        """
        import uiautomator2 as u2

        steps: list[Step] = []

        def step(name: str):
            log.info("step: %s", name)
            if on_step:
                on_step(name)

        step("wake_and_unlock")
        self.wake_and_unlock()
        time.sleep(1)

        step("set_location")
        self.set_location(lat, lon)
        time.sleep(3)  # let the fake fix settle

        step("open_allsports")
        self.open_allsports()
        time.sleep(6)

        d = u2.connect(self.serial)
        steps.append(Step("allsports_home", self.screenshot()))

        step("new_checkin_tab")
        # Bottom-nav tab opening the venue list. EN and RU labels seen in the
        # wild; calibrate via /dump if none matches.
        new_checkin_re = "(?i).*(check.?in|чек.?ин|посещение|визит).*"
        if not d(descriptionMatches=new_checkin_re).click_exists(timeout=5):
            d(textMatches=new_checkin_re).click_exists(timeout=5)
        time.sleep(3)
        steps.append(Step("venue_list", self.screenshot()))

        step("select_venue")
        # The venue list is geo-filtered; find our place by name and tap it.
        name_re = "(?i).*" + re.escape(place_name) + ".*"
        found = d(textMatches=name_re).click_exists(timeout=10)
        if not found and d(scrollable=True).exists:
            d(scrollable=True).scroll.toBeginning()
            if d(scrollable=True).scroll.to(textMatches=name_re):
                found = d(textMatches=name_re).click_exists(timeout=3)
        steps.append(Step("venue_selected", self.screenshot()))
        if not found:
            raise DeviceError(f"venue '{place_name}' not found in geo list")

        step("tap_checkin")
        time.sleep(2)
        # Supplier bottom sheet -> Check in button
        checkin_btn_re = "(?i).*(check.?in|отметиться|отметить|чек.?ин).*"
        if not d(textMatches=checkin_btn_re).click_exists(timeout=8):
            raise DeviceError("'Check in' button not found")
        time.sleep(4)

        step("confirm")
        # Possible extra dialog ("Choose a check-in method" / confirm).
        if d(textMatches="(?i).*(choose a check-in method|способ).*").exists:
            d(textMatches="(?i).*(location|geo|near me|гео|локац).*").click_exists(timeout=5)
            time.sleep(3)
        if d(textMatches="(?i).*(confirm|yes|ok|подтвердить|да|ок).*").exists:
            d(textMatches="(?i).*(confirm|yes|ok|подтвердить|да|ок).*").click_exists(timeout=3)
            time.sleep(3)

        step("result")
        steps.append(Step("result", self.screenshot()))
        return steps

    def dump_ui(self) -> str:
        """Current window hierarchy for selector calibration."""
        import uiautomator2 as u2

        d = u2.connect(self.serial)
        return d.dump_hierarchy()
