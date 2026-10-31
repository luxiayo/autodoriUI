import logging
import sys
import tempfile
from pathlib import Path

import requests
from diskcache import Cache
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

_logger = logging.getLogger("BestdoriAPI")


def _cache_directory() -> Path:
    """
    Absolute cache location next to the executable.

    This used to be the relative path "cache", resolved against the current
    working directory. Windows starts autostart entries with an unrelated CWD
    (the tray start at boot), so diskcache then tried to create
    <cwd>\\cache - C:\\Windows\\System32\\cache for a machine-wide Run key -
    and the import died with PermissionError before the UI could even open.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    primary = base / "cache"
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    except OSError as e:
        fallback = Path(tempfile.gettempdir()) / "autodoriUI-cache"
        fallback.mkdir(parents=True, exist_ok=True)
        _logger.warning(f"Cannot use {primary} for the cache ({e}); falling back to {fallback}.")
        return fallback


class BestdoriAPI:
    base = "https://bestdori.com/api"
    _logger = _logger
    _cache = Cache(str(_cache_directory()))
    _session = requests.Session()
    _adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],
            connect=5,
            read=5,
        )
    )
    _session.mount("http://", _adapter)
    _session.mount("https://", _adapter)

    @staticmethod
    def _fetch_and_cache(url, cache_name, expire=None):
        if cache_ := BestdoriAPI._cache.get(cache_name):
            BestdoriAPI._logger.info(f"Cache hit for {cache_name}")
            return cache_
        else:
            response = BestdoriAPI._session.get(url).json()
            BestdoriAPI._cache.set(cache_name, response, expire=expire)
            BestdoriAPI._logger.info(f"Cache set for {cache_name}")
            return response

    @staticmethod
    def get_song_list():
        url = BestdoriAPI.base + "/songs/all.5.json"
        return BestdoriAPI._fetch_and_cache(url, "allsongs", expire=3600 * 1)

    @staticmethod
    def get_chart(song_id: str, difficulty: str):
        cacheid = f"{song_id}-{difficulty}"
        url = BestdoriAPI.base + f"/charts/{song_id}/{difficulty}.json"
        return BestdoriAPI._fetch_and_cache(url, cacheid)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    songlist = BestdoriAPI.get_song_list()
    chart = BestdoriAPI.get_chart(1, "easy")
    pass
