"""Device control over network ADB: Lexa Fake GPS intents + UI automation in Allsports.

Verified against com.lexa.fakegps 2.1.2 (decompiled):
  FakeGPSService handles intents com.lexa.fakegps.START / com.lexa.fakegps.STOP.
  START extras: "lat" and "long" as string extras, optional bool "no_history".
"""

from __future__ import annotations

import io
import logging
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

ADB = "adb"
FAKEGPS_START = "com.lexa.fakegps.START"
FAKEGPS_STOP = "com.lexa.fakegps.STOP"
FAKEGPS_SERVICE = "com.lexa.fakegps/.FakeGPSService"


class DeviceError(RuntimeError):
    pass


@dataclass
class Step:
    name: str
    screenshot: bytes | None = None


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
        self._adb(
            "shell", "am", "start-foreground-service",
            "-a", FAKEGPS_START,
            "-n", FAKEGPS_SERVICE,
            "--es", "lat", str(lat),
            "--es", "long", str(lon),
        )

    def clear_location(self) -> None:
        self._adb(
            "shell", "am", "start-foreground-service",
            "-a", FAKEGPS_STOP,
            "-n", FAKEGPS_SERVICE,
        )

    # ---------- apps ----------

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

        step("set_location")
        self.set_location(lat, lon)
        time.sleep(3)  # let the fake fix settle

        step("open_allsports")
        self.open_allsports()
        time.sleep(6)

        d = u2.connect(self.serial)

        step("new_checkin_tab")
        # Bottom-nav "New check-in" tab. Try resource-id first, then text.
        if not d(description="New check-in").click_exists(timeout=5):
            d(text="New check-in").click_exists(timeout=5)
        time.sleep(3)

        step("select_venue")
        # The venue list is geo-filtered; find our place by name and tap it.
        found = d(textContains=place_name).click_exists(timeout=10)
        if not found:
            d(scrollable=True).scroll.toBeginning()
            found = d(scrollable=True).scroll.to(textContains=place_name)
            if found:
                d(textContains=place_name).click_exists(timeout=3)
        steps.append(Step("venue_selected", self.screenshot()))
        if not found:
            raise DeviceError(f"venue '{place_name}' not found in geo list")

        step("tap_checkin")
        time.sleep(2)
        # Supplier bottom sheet -> Check in button
        if not d(textMatches="(?i)check.?in").click_exists(timeout=8):
            raise DeviceError("'Check in' button not found")
        time.sleep(4)

        step("confirm")
        # Possible extra dialog ("Choose a check-in method" / confirm).
        if d(textMatches="(?i)choose a check-in method").exists:
            d(textMatches="(?i)(location|geo|near me)").click_exists(timeout=5)
            time.sleep(3)
        if d(textMatches="(?i)(confirm|yes|ok)").exists:
            d(textMatches="(?i)(confirm|yes|ok)").click_exists(timeout=3)
            time.sleep(3)

        step("result")
        steps.append(Step("result", self.screenshot()))
        return steps

    def dump_ui(self) -> str:
        """Current window hierarchy for selector calibration."""
        import uiautomator2 as u2

        d = u2.connect(self.serial)
        return d.dump_hierarchy()
