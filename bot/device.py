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
import threading
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


class CheckinCancelled(DeviceError):
    """Raised inside the check-in flow when a newer request supersedes it."""


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
    def __init__(self, serial: str, allsports_pkg: str = "by.allsports.holder",
                 pin: str = ""):
        self.serial = serial
        self.allsports_pkg = allsports_pkg
        self.pin = pin
        # (req_lat, req_lon, act_lat, act_lon, offset_m) of the last spoof
        self.last_spoof: tuple | None = None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Abort the in-flight check-in at its next wait point."""
        self._cancel.set()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise CheckinCancelled("superseded by a newer request")

    # ---------- raw adb ----------

    def _adb(self, *args: str, timeout: int = 30) -> str:
        cmd = [ADB, "-s", self.serial, *args]
        log.info("adb %s", " ".join(args))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise DeviceError(f"adb {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def connect(self) -> str:
        out = self._try_connect()
        if "connected" not in out:
            # Possibly a stale adb-server session — drop and retry once.
            subprocess.run(
                [ADB, "disconnect", self.serial],
                capture_output=True, text=True, timeout=15,
            )
            out = self._try_connect()
        if "connected" not in out:
            raise DeviceError(f"adb connect: {out}")
        return out

    def _try_connect(self) -> str:
        r = subprocess.run(
            [ADB, "connect", self.serial], capture_output=True, text=True, timeout=15
        )
        return (r.stdout + r.stderr).strip()

    # ---------- fake gps ----------

    def set_location(self, lat: float, lon: float, jitter: bool = True) -> None:
        if jitter:
            actual_lat, actual_lon, offset_m = jitter_location(lat, lon)
            log.info(
                "location jitter: %.1fm; requested=%.7f,%.7f actual=%.7f,%.7f",
                offset_m,
                lat,
                lon,
                actual_lat,
                actual_lon,
            )
        else:
            actual_lat, actual_lon, offset_m = lat, lon, 0.0
            log.info("location set exact: %.7f,%.7f", lat, lon)
        self.last_spoof = (lat, lon, actual_lat, actual_lon, offset_m)
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
        cmds = ["input keyevent KEYCODE_WAKEUP", "sleep 1"]
        if self.pin:
            # Swipe up to reveal the PIN field, type it, confirm.
            cmds += ["input swipe 540 1800 540 600", "sleep 1",
                     f"input text '{self.pin}'", "input keyevent 66",
                     "sleep 1"]
        cmds.append("wm dismiss-keyguard")
        self._adb("shell", "; ".join(cmds))

    def open_allsports(self) -> None:
        self._adb(
            "shell", "monkey", "-p", self.allsports_pkg,
            "-c", "android.intent.category.LAUNCHER", "1",
        )

    def close_allsports(self) -> None:
        self._adb("shell", "am", "force-stop", self.allsports_pkg)

    def stop_visit(self) -> None:
        """Cancel the spoof and close Allsports."""
        self.clear_location()
        self.close_allsports()

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
                on_step=None, service: str | None = None,
                jitter: bool = True) -> list[Step]:
        """Drive the Allsports check-in. Returns per-step screenshots.

        on_step(name) is called after each stage for progress reporting.
        Selectors are text-based; calibrate via /shot + uiautomator dump on
        the real device if a stage fails.
        """
        import uiautomator2 as u2

        self._cancel.clear()
        steps: list[Step] = []

        def step(name: str):
            log.info("step: %s", name)
            self._check_cancel()
            if on_step:
                on_step(name)

        step("wake_and_unlock")
        self.wake_and_unlock()
        time.sleep(1)

        step("set_location")
        self.set_location(lat, lon, jitter=jitter)
        time.sleep(8)  # let the fake fix settle before the app reads it

        step("open_allsports")
        self.open_allsports()
        time.sleep(6)

        d = u2.connect(self.serial)
        steps.append(Step("allsports_home", self.screenshot()))

        # Flutter exposes labels via content-desc (often multi-line, e.g.
        # "Новый визит\nВкладка 2 из 3"); `text` stays empty. UiSelector
        # regexes must match the WHOLE string, so (?s) is needed to let
        # `.*` cross the \n.
        def tap(pattern: str, timeout: float) -> bool:
            return (
                d(descriptionMatches=pattern).click_exists(timeout=timeout)
                or d(textMatches=pattern).click_exists(timeout=3)
            )

        step("new_checkin_tab")

        # Venue desc is "Название\nАдрес". Strict match only: the row's
        # normalized title must equal the saved name — no fuzzy fallback,
        # so a near-miss can never tap the wrong venue.
        norm = self._norm
        want = norm(place_name)
        seen: set[str] = set()

        def find_venue():
            def match(title: str) -> bool:
                n = norm(title)
                if n:
                    seen.add(n)
                return n == want
            return self._xml_find(d, match)

        # «Объектов поблизости не найдено» — the app never picked up the
        # spoofed fix; waiting won't help, only an app restart does.
        empty_re = "(?i).*объектов поблизости не найдено.*"

        def list_empty() -> bool:
            return (d(descriptionMatches=empty_re).exists
                    or d(textMatches=empty_re).exists)

        target = None
        for app_try in range(3):
            if app_try:
                step(f"restart_app_{app_try + 1}")  # reports progress too
                self.close_allsports()
                time.sleep(2)
                self.set_location(lat, lon, jitter=jitter)
                time.sleep(3)
                self.open_allsports()
                time.sleep(6)
            try:
                self._open_geo_list(d)
            except DeviceError:
                if app_try < 2:
                    continue
                raise
            time.sleep(3)
            steps.append(Step("venue_list", self.screenshot()))

            step("select_venue")
            # The list loads asynchronously after the location changes —
            # poll for a while, then scroll to the bottom.
            deadline = time.time() + 20
            while target is None and time.time() < deadline and not list_empty():
                self._check_cancel()
                target = find_venue()
                if target is None:
                    time.sleep(1)
            for _ in range(6):
                if (target is not None or list_empty()
                        or not d(scrollable=True).exists):
                    break
                before = d.dump_hierarchy()
                d(scrollable=True).scroll.forward()
                time.sleep(1)
                self._check_cancel()
                target = find_venue()
                if d.dump_hierarchy() == before:
                    break
            if target is not None:
                break
            # Miss or an empty list — restart the app on the next pass.
        if target is not None:
            d.click(*target)
        steps.append(Step("venue_selected", self.screenshot()))
        if target is None:
            listed = "\n".join(f"  - {t}" for t in sorted(seen)) or "  (пусто)"
            raise DeviceError(
                f"venue '{place_name}' not found in geo list. Saw:\n{listed}")

        if service is not None:
            step("select_service")
            # «Выбор активности»: strict title match on the saved service name.
            want_svc = norm(service)
            svc = None
            deadline = time.time() + 15
            while time.time() < deadline:
                self._check_cancel()
                svc = self._xml_find(d, lambda t: norm(t) == want_svc)
                if svc is not None:
                    break
                time.sleep(1)
            steps.append(Step("service_selected", self.screenshot()))
            if svc is None:
                raise DeviceError(f"service '{service}' not found")
            d.click(*svc)
            time.sleep(3)

        step("wait_for_confirmation")
        # The service tap often submits the request directly — the app
        # then shows «Запрос на создание визита отправлен… ожидайте
        # подтверждения администратора» and no check-in button at all.
        # Poll for that waiting state first; only tap check-in if it
        # never appears.
        waiting_re = ("(?is).*(запрос.*отправлен|ожидайте\\s*подтверждени|"
                      "истечени.*времени\\s*ожидания|подтверждени.*администратор).*")
        sent = False
        deadline = time.time() + 10
        while time.time() < deadline:
            self._check_cancel()
            if (d(descriptionMatches=waiting_re).exists
                    or d(textMatches=waiting_re).exists):
                sent = True
                break
            time.sleep(1)
        steps.append(Step("checkin_state", self.screenshot()))

        if not sent:
            step("tap_checkin")
            time.sleep(2)
            # Supplier bottom sheet -> Check in button
            checkin_btn_re = "(?is).*(check.?in|отметиться|отметить|чек.?ин|записаться|забронировать|посетить).*"
            if not tap(checkin_btn_re, timeout=8):
                raise DeviceError("'Check in' button not found")
            time.sleep(4)

            step("confirm")
            # Possible extra dialog ("Choose a check-in method" / confirm).
            if d(descriptionMatches="(?is).*(choose a check-in method|способ).*").exists \
                    or d(textMatches="(?is).*(choose a check-in method|способ).*").exists:
                tap("(?is).*(location|geo|near me|гео|локац).*", timeout=5)
                time.sleep(3)
            confirm_re = "(?is).*(confirm|yes|ok|подтвердить|да\b|ок\b).*"
            tap(confirm_re, timeout=3)
            time.sleep(3)

        # After the request is sent, watch the admin-confirmation outcome:
        # «Время ожидания истекло» (~2.5 min window) or a confirmed visit.
        step("await_outcome")
        expired_re = "(?is).*время\s*ожидани.*истек.*"
        confirm_re = "(?i).*хорошей\s*тренировки.*"
        outcome = "sent"
        deadline = time.time() + 200
        while time.time() < deadline:
            self._check_cancel()
            if (d(descriptionMatches=expired_re).exists
                    or d(textMatches=expired_re).exists):
                outcome = "expired"
                break
            if (d(descriptionMatches=confirm_re).exists
                    or d(textMatches=confirm_re).exists):
                outcome = "confirmed"
                break
            time.sleep(5)
        steps.append(Step(f"outcome_{outcome}", self.screenshot()))

        step("result")
        steps.append(Step("result", self.screenshot()))
        step("close_app")
        self.close_allsports()
        return steps

    @staticmethod
    def _xml_find(d, matcher):
        """Dump the hierarchy ONCE and return (x, y) of the first clickable
        node whose content-desc/text first line passes matcher(title).

        Way faster than iterating d(clickable=True) — every el.info is an
        HTTP round trip to the phone, while this is a single dump."""
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(d.dump_hierarchy())
        except ET.ParseError:
            return None
        for node in root.iter("node"):
            if node.get("clickable") != "true":
                continue
            desc = node.get("content-desc") or node.get("text") or ""
            title = desc.splitlines()[0].strip() if desc else ""
            if not title or not matcher(title):
                continue
            b = re.findall(r"\d+", node.get("bounds") or "")
            if len(b) != 4:
                continue
            l, t, r, bo = (int(v) for v in b)
            return (l + r) // 2, (t + bo) // 2
        return None

    # ---------- venue discovery ----------

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(
            r"\s+", " ",
            re.sub(r"[^\w\s]", " ", s).lower().replace("ё", "е"),
        ).strip()

    @staticmethod
    def _tap(d, pattern: str, timeout: float) -> bool:
        return (
            d(descriptionMatches=pattern).click_exists(timeout=timeout)
            or d(textMatches=pattern).click_exists(timeout=3)
        )

    def _open_geo_list(self, d) -> None:
        """Bottom-nav 'Новый визит' -> 'Вход по локации' venue list."""
        # 'новый визит' can't collide with body copy like 'Осталось 4 визита'.
        self._tap(d, "(?is).*(new (visit|check.?in)|новый визит).*", timeout=5)
        time.sleep(3)
        loc_re = "(?is).*(вход по локац|by location|enter by location).*"
        if not self._tap(d, loc_re, timeout=8):
            raise DeviceError("'Enter by location' option not found")
        time.sleep(3)

    def venues_near(
        self, lat: float, lon: float, jitter: bool = True
    ) -> tuple[list[tuple[str, str]], bytes]:
        """Set the location (jitter per setting), open the geo list and collect
        the venues the app offers there. Returns ([(name, detail)], screenshot)
        where detail is the row's second line (address or distance)."""
        import uiautomator2 as u2

        self.wake_and_unlock()
        self.set_location(lat, lon, jitter=jitter)
        time.sleep(3)
        self.open_allsports()
        time.sleep(6)

        d = u2.connect(self.serial)
        self._open_geo_list(d)
        time.sleep(2)

        names: list[tuple[str, str]] = []
        seen = set()

        def collect() -> None:
            for el in d(clickable=True):
                desc = (el.info.get("contentDescription") or "").splitlines()
                if len(desc) < 2:
                    continue
                second = desc[1].strip().lower()
                # Venue rows: title + address ("г. Минск, ...") or distance.
                if second.startswith("г.") or "м от вас" in second or "км от вас" in second:
                    title = desc[0].strip()
                    if title and title not in seen:
                        seen.add(title)
                        names.append((title, desc[1].strip()))

        collect()
        shot = self.screenshot()
        if d(scrollable=True).exists:
            for _ in range(6):
                before = d.dump_hierarchy()
                d(scrollable=True).scroll.forward()
                time.sleep(1)
                if d.dump_hierarchy() == before:
                    break
                collect()

        return names, shot

    def services_of_venue(self, place_name: str, lat: float, lon: float,
                            jitter: bool = True) -> list[str]:
        """Open the venue in the geo list and collect its «Выбор активности»
        service titles. Navigation is restored with Back afterwards."""
        import uiautomator2 as u2

        self.wake_and_unlock()
        self.set_location(lat, lon, jitter=jitter)
        time.sleep(3)
        self.open_allsports()
        time.sleep(6)

        d = u2.connect(self.serial)
        self._open_geo_list(d)
        time.sleep(2)

        norm = self._norm
        target = None
        deadline = time.time() + 20
        while time.time() < deadline:
            for el in d(clickable=True):
                desc = el.info.get("contentDescription") or ""
                if norm(desc.splitlines()[0] if desc else "") == norm(place_name):
                    target = el
                    break
            if target is not None:
                break
            time.sleep(1)
        if target is None:
            raise DeviceError(f"venue '{place_name}' not found in geo list")
        target.click()
        time.sleep(4)

        skip = {
            "объекты", "новый визит", "профиль", "назад",
            "вход по локации", "выбор активности",
        }
        services: list[str] = []
        for el in d(clickable=True):
            desc = (el.info.get("contentDescription") or "").splitlines()
            if not desc:
                continue
            title = desc[0].strip()
            body = norm(" ".join(desc))
            if not title or "вкладка" in body or body in skip:
                continue
            if title not in services:
                services.append(title)

        d.press("back")
        return services

    def dump_ui(self) -> str:
        """Current window hierarchy for selector calibration."""
        import uiautomator2 as u2

        d = u2.connect(self.serial)
        return d.dump_hierarchy()
