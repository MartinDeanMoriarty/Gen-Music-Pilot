"""Current weather from OpenWeatherMap, as a TTS-ready German phrase.

The story prompt carries a line like "Achtzehn Grad und leicht bewoelkt in
Koeln" -- temperature spelled out (:mod:'pilot.germanum') so the TTS voice
speaks it cleanly, description straight from the API's German localisation.

The result is cached for "ttl_minutes" (the old code refetched at most once an
hour). With no API key, or on any fetch/parse failure with nothing cached, it
returns "nicht verfuegbar" -- the DJ handles that gracefully.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import requests

from pilot.config import WeatherConfig
from pilot.germanum import cardinal

log = logging.getLogger("pilot.weather")

_API = "https://api.openweathermap.org/data/2.5/weather"
UNAVAILABLE = "nicht verfügbar"


def format_weather(temp_c: int, description: str, location: str) -> str:
    return f"{cardinal(temp_c)} Grad und {description.strip()} in {location}"


class Weather:
    def __init__(
        self,
        config: WeatherConfig,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        request_timeout: float = 10.0,
    ) -> None:
        self._cfg = config
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._clock = clock
        self._timeout = request_timeout
        self._ttl = max(1, config.ttl_minutes) * 60
        self._cached: str | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def current(self) -> str:
        """The weather phrase for a prompt. Never raises."""
        if not self._cfg.enabled:
            return UNAVAILABLE
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._fetched_at < self._ttl:
                return self._cached
            fresh = self._fetch()
            if fresh is not None:
                self._cached = fresh
                self._fetched_at = now
                return fresh
            return self._cached or UNAVAILABLE  # serve stale rather than nothing

    # -- internals --------------------------------------------

    def _fetch(self) -> str | None:
        params = {
            "q": self._cfg.location,
            "appid": self._cfg.apikey,
            "units": "metric",
            "lang": "de",
        }
        try:
            r = self._session.get(_API, params=params, timeout=self._timeout)
        except requests.RequestException as e:
            log.warning("weather fetch failed: %s", e)
            return None
        if r.status_code != 200:
            log.warning("weather API returned HTTP %s: %s", r.status_code, r.text[:160])
            return None
        try:
            data = r.json()
            temp = round(float(data["main"]["temp"]))
            description = str(data["weather"][0]["description"])
        except (ValueError, KeyError, IndexError, TypeError) as e:
            log.warning("weather response unparseable: %s", e)
            return None
        return format_weather(temp, description, self._cfg.location)
