"""In-process fish-species researcher.

The upstream project ran the researcher as a separate Apify actor reached over
HTTP. The actual research logic in :mod:`research` is pure ``httpx`` +
``BeautifulSoup`` with no Apify dependency, so here we run it in-process and
drop the subprocess/HTTP hop entirely.

Lookups prefer the bundled species snapshots in ``species_data/`` (so the demo
works offline and without hammering FishBase), and fall back to live scraping
for anything not bundled.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "species_data"


def _index_key(s: str) -> str:
    return (s or "").strip().lower()


class Researcher:
    """Drop-in replacement for the upstream ``ResearcherClient``.

    Exposes the same ``async research_species(name) -> dict`` plus ``start`` /
    ``stop`` lifecycle hooks so the manager/agent code is unchanged.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._bundled: dict[str, dict] = {}
        self._load_bundled()

    def _load_bundled(self) -> None:
        if not _DATA_DIR.exists():
            return
        for path in sorted(_DATA_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            species = data.get("species") or {}
            for field in ("input_name", "common_name", "scientific_name", "genus"):
                key = _index_key(species.get(field, ""))
                if key:
                    self._bundled.setdefault(key, data)
        log.info("Aquarium researcher: %d bundled species snapshots loaded", len(set(id(v) for v in self._bundled.values())))

    async def start(self) -> None:  # pragma: no cover - lifecycle parity
        pass

    async def stop(self) -> None:  # pragma: no cover - lifecycle parity
        pass

    async def health_check(self) -> bool:
        return True

    async def research_species(self, name: str) -> dict:
        key = _index_key(name)
        if key in self._cache:
            return self._cache[key]
        if key in self._bundled:
            self._cache[key] = self._bundled[key]
            return self._bundled[key]

        # Fall back to live scraping for un-bundled species.
        try:
            from .research import research_species as _live
        except Exception as exc:  # pragma: no cover - optional dep guard
            raise RuntimeError(
                f"'{name}' is not in the bundled species set and live research is "
                f"unavailable ({exc}). Install 'beautifulsoup4' to enable scraping."
            ) from exc

        log.info("Aquarium researcher: live lookup for %r", name)
        data = await _live(name)
        self._cache[key] = data
        return data
