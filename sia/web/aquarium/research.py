"""
research.py  —  Fish species research logic (FishBase + Wikipedia).

Provides a single top-level coroutine:
    result = await research_species(species_name)  -> dict

All scraping functions are pure (no Apify SDK dependency) so they can be
called from both the Apify batch actor and the aiohttp standby server.
"""
from __future__ import annotations

import re
import asyncio
import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locomotion lookup tables
# ---------------------------------------------------------------------------

_LOCO_BY_FAMILY: dict[str, tuple[str, float]] = {
    'scombridae':      ('thunniform',     0.05),
    'istiophoridae':   ('thunniform',     0.05),
    'xiphiidae':       ('thunniform',     0.05),
    'carangidae':      ('carangiform',    0.35),
    'lutjanidae':      ('carangiform',    0.35),
    'sparidae':        ('carangiform',    0.35),
    'serranidae':      ('carangiform',    0.35),
    'labridae':        ('carangiform',    0.35),
    'haemulidae':      ('carangiform',    0.35),
    'centropomidae':   ('carangiform',    0.35),
    'sciaenidae':      ('carangiform',    0.35),
    'salmonidae':      ('subcarangiform', 0.55),
    'esocidae':        ('subcarangiform', 0.55),
    'gadidae':         ('subcarangiform', 0.55),
    'percidae':        ('subcarangiform', 0.55),
    'cichlidae':       ('subcarangiform', 0.55),
    'clupeidae':       ('subcarangiform', 0.55),
    'anguillidae':     ('anguilliform',   0.90),
    'muraenidae':      ('anguilliform',   0.95),
    'ophichthidae':    ('anguilliform',   0.90),
    'congridae':       ('anguilliform',   0.90),
    'ostraciidae':     ('ostraciiform',   0.02),
    'tetraodontidae':  ('ostraciiform',   0.05),
    'diodontidae':     ('ostraciiform',   0.05),
    'balistidae':      ('labriform',      0.05),
    'acanthuridae':    ('labriform',      0.10),
}

_LOCO_BY_ORDER: dict[str, tuple[str, float]] = {
    'anguilliformes':    ('anguilliform',   0.90),
    'tetraodontiformes': ('ostraciiform',   0.05),
    'salmoniformes':     ('subcarangiform', 0.55),
    'clupeiformes':      ('subcarangiform', 0.50),
    'perciformes':       ('subcarangiform', 0.50),
    'scorpaeniformes':   ('subcarangiform', 0.50),
}

_LOCO_DESCRIPTIONS = {
    'thunniform':     'Propulsion almost entirely from the lunate caudal fin; body is nearly rigid.',
    'carangiform':    'Rear third of body undulates; efficient for sustained fast swimming.',
    'subcarangiform': 'Rear half of body undulates; the most common teleost swimming mode.',
    'anguilliform':   'Full body undulates in sinusoidal waves; excellent low-speed maneuverability.',
    'ostraciiform':   'Rigid body; propelled by median and paired fins (MPF swimming).',
    'labriform':      'Pectoral fins are the primary thrust source; body mostly stationary.',
}

_TAIL_SHAPES = {
    'thunniform':     'lunate',
    'carangiform':    'forked',
    'subcarangiform': 'forked',
    'anguilliform':   'rounded',
    'ostraciiform':   'rounded',
    'labriform':      'rounded',
}

_LOCO_TYPE_TO_UNDULATION = {
    'thunniform': 0.05, 'carangiform': 0.35, 'subcarangiform': 0.55,
    'anguilliform': 0.90, 'ostraciiform': 0.02, 'labriform': 0.05,
}

_FISHSIM_BY_LOCO = {
    'thunniform':     {'stroke_period': 14, 'max_tail_angle': 20, 'drag': 0.5},
    'carangiform':    {'stroke_period': 22, 'max_tail_angle': 30, 'drag': 0.6},
    'subcarangiform': {'stroke_period': 28, 'max_tail_angle': 36, 'drag': 0.7},
    'anguilliform':   {'stroke_period': 36, 'max_tail_angle': 50, 'drag': 0.9},
    'ostraciiform':   {'stroke_period': 40, 'max_tail_angle': 10, 'drag': 1.2},
    'labriform':      {'stroke_period': 32, 'max_tail_angle': 12, 'drag': 0.8},
}

# ---------------------------------------------------------------------------
# HTTP headers
# ---------------------------------------------------------------------------

HEADERS_HTML = {
    'User-Agent': 'FishResearchBot/1.0 (fish-3d-pipeline; contact@fishresearch.example)',
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}

HEADERS_JSON = {
    'User-Agent': 'FishResearchBot/1.0 (fish-3d-pipeline; contact@fishresearch.example)',
    'Accept': 'application/json',
}

# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

_SCI_NAME_RE      = re.compile(r'^[A-Z][a-z]+ [a-z]+$')
_WIKI_GENUS_RE    = re.compile(r'\|\s*genus\s*=\s*([A-Z][a-z]+)', re.IGNORECASE)
_WIKI_SPP_RE      = re.compile(r'\|\s*species\s*=\s*([a-z]+)', re.IGNORECASE)
_WIKI_BINOMIAL_RE = re.compile(
    r"'''''([A-Z][a-z]+ [a-z]+)'''''|binomial\s*=\s*([A-Z][a-z]+ [a-z]+)", re.IGNORECASE
)

_SECTIONS_OF_INTEREST = {
    'behavior', 'feeding', 'diet', 'locomotion',
    'description', 'anatomy', 'predation', 'biology', 'ecology',
}

_FOOD_WORDS = [
    'fish', 'squid', 'crustaceans', 'plankton', 'krill', 'shrimp', 'crab',
    'herring', 'mackerel', 'sardine', 'anchovy', 'invertebrates', 'algae',
    'zooplankton', 'copepods', 'jellyfish', 'eels', 'octopus',
]

_ACTIVE_PREY  = {'fish', 'squid', 'herring', 'mackerel', 'sardine', 'anchovy', 'eels', 'octopus'}
_PASSIVE_PREY = {'plankton', 'krill', 'zooplankton', 'copepods'}


async def _extract_sci_name_from_page(
    client: httpx.AsyncClient, page_title: str
) -> str | None:
    """Return scientific name from a Wikipedia page's wikitext, or None."""
    r = await client.get(
        'https://en.wikipedia.org/w/api.php',
        params={
            'action': 'parse', 'page': page_title,
            'prop': 'wikitext', 'format': 'json',
        },
        headers=HEADERS_JSON,
    )
    wikitext = r.json().get('parse', {}).get('wikitext', {}).get('*', '')

    genus_m   = _WIKI_GENUS_RE.search(wikitext)
    species_m = _WIKI_SPP_RE.search(wikitext)
    if genus_m and species_m:
        return f'{genus_m.group(1)} {species_m.group(1)}'

    binomial_m = _WIKI_BINOMIAL_RE.search(wikitext)
    if binomial_m:
        return (binomial_m.group(1) or binomial_m.group(2)).strip()

    return None


async def resolve_scientific_name(
    client: httpx.AsyncClient, species_name: str
) -> tuple[str, str]:
    """Return (wikipedia_page_title, scientific_name).

    Tries all search results until one yields a valid taxobox/speciesbox.
    Falls back directly if input already looks like a scientific name.
    """
    if _SCI_NAME_RE.match(species_name.strip()):
        return species_name.replace(' ', '_'), species_name.strip()

    r = await client.get(
        'https://en.wikipedia.org/w/api.php',
        params={
            'action': 'query', 'list': 'search',
            'srsearch': species_name, 'srlimit': 8, 'format': 'json',
        },
        headers=HEADERS_JSON,
    )
    results = r.json().get('query', {}).get('search', [])
    if not results:
        raise ValueError(f'No Wikipedia results for: {species_name!r}')

    # Try each result in order until we find one with a scientific name
    for result in results:
        page_title = result['title']
        sci = await _extract_sci_name_from_page(client, page_title)
        if sci:
            log.info(f'Resolved {species_name!r} → {sci!r} via page {page_title!r}')
            return page_title, sci

    raise ValueError(
        f'Could not find a species page for {species_name!r}. '
        'Try a more specific name (e.g. "ocellaris clownfish" or "Amphiprion ocellaris").'
    )


async def scrape_fishbase(client: httpx.AsyncClient, scientific_name: str) -> dict[str, Any]:
    parts = scientific_name.split()
    url   = f'https://www.fishbase.se/summary/{parts[0]}-{parts[1]}.html'
    resp  = await client.get(url, follow_redirects=True, headers=HEADERS_HTML)
    soup  = BeautifulSoup(resp.text, 'html.parser')
    text  = soup.get_text(' ', strip=True)
    data: dict[str, Any] = {}

    title_tag = soup.find('title')
    if title_tag:
        m = re.search(r',\s*(.+?)\s*[:\|]', title_tag.get_text(strip=True))
        if m:
            data['common_name'] = re.sub(r'\s+', ' ', m.group(1)).strip()

    family_link = (
        soup.find('a', string=re.compile(r'[A-Z][a-z]+idae'))
        or soup.find('a', string=re.compile(r'[A-Z][a-z]+inae'))
    )
    if family_link:
        data['family'] = family_link.get_text(strip=True)

    order_link = soup.find('a', string=re.compile(r'[A-Z][a-z]+iformes'))
    if order_link:
        data['order'] = order_link.get_text(strip=True)

    m = re.search(r'\bActinopteri\b|\bElasmobranchii\b|\bMyxini\b|\bPetromyzontida\b', text)
    if m:
        data['class_'] = m.group(0)

    for field, pattern in [
        ('max_length_cm', r'Max\.?\s*length\s*[:\s]+([\d.]+)\s*cm'),
        ('max_weight_kg', r'Max\.?\s*weight\s*[:\s]+([\d.]+)\s*kg'),
        ('max_weight_g',  r'Max\.?\s*weight\s*[:\s]+([\d.]+)\s*g\b'),
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            data[field] = float(m.group(1))

    if 'max_weight_g' in data and 'max_weight_kg' not in data:
        data['max_weight_kg'] = data.pop('max_weight_g') / 1000

    m = re.search(r'depth\s+range[^:]*?(\d+)\s*[-–]\s*(\d+)\s*m', text, re.IGNORECASE)
    if m:
        data['depth_min_m'] = int(m.group(1))
        data['depth_max_m'] = int(m.group(2))

    for field, pattern in [
        ('dorsal_spines', r'Dorsal\s+spines[:\s]+([\d]+)'),
        ('dorsal_rays',   r'Dorsal\s+soft\s+rays[:\s]+([\d]+)'),
        ('anal_spines',   r'Anal\s+spines[:\s]+([\d]+)'),
        ('anal_rays',     r'Anal\s+soft\s+rays[:\s]+([\d]+)'),
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            data[field] = int(m.group(1))

    m = re.search(r'Trophic\s+level[^:]*:[^\d]*([\d.]+)', text.replace('\xa0', ' '), re.IGNORECASE)
    if m:
        data['trophic_level'] = float(m.group(1))

    m = re.search(r'Body\s+shape.*?:\s*([^;\n]{10,120})', text, re.IGNORECASE)
    if m:
        data['body_shape_desc'] = re.sub(r'\s+', ' ', m.group(1)).strip()

    short_desc = soup.find('h1', string=re.compile(r'Short description', re.IGNORECASE))
    if short_desc:
        for sibling in short_desc.find_all_next(['p', 'td', 'h1'])[:6]:
            t = sibling.get_text(' ', strip=True)
            _color_re = r'\b(blue|green|silver|gold|dark|pale|spotted|striped|color|colour)\b'
            if re.search(_color_re, t, re.IGNORECASE) and len(t) > 30:
                data['coloration_raw'] = t[:300]
                break

    m = re.search(r'Teeth[:\s]+([^.]{10,150}\.)', text, re.IGNORECASE)
    if m:
        data['teeth_desc'] = m.group(1).strip()

    data['dangerous'] = bool(
        re.search(r'\b(?:dangerous|harmful|venomous|poisonous)\b', text, re.IGNORECASE)
    )

    return data


async def scrape_wikipedia(client: httpx.AsyncClient, page_title: str) -> dict[str, Any]:
    wiki_url = f'https://en.wikipedia.org/wiki/{page_title.replace(" ", "_")}'
    data: dict[str, Any] = {'wikipedia_url': wiki_url}

    r = await client.get(
        f'https://en.wikipedia.org/api/rest_v1/page/summary/{page_title.replace(" ", "_")}',
        headers=HEADERS_JSON,
    )
    if r.status_code == 200:
        summary = r.json()
        data['intro'] = summary.get('extract', '')[:600]
        orig  = summary.get('originalimage')
        thumb = summary.get('thumbnail')
        if orig:
            data['wiki_image_url']  = orig['source']
            data['wiki_image_dims'] = (orig['width'], orig['height'])
        if thumb:
            data['wiki_thumbnail_url'] = thumb['source']

    r2 = await client.get(
        'https://en.wikipedia.org/w/api.php',
        params={
            'action': 'parse', 'page': page_title,
            'prop': 'sections|text', 'format': 'json',
        },
        headers=HEADERS_JSON,
    )
    parsed    = r2.json().get('parse', {})
    sections  = parsed.get('sections', [])
    full_html = parsed.get('text', {}).get('*', '')
    soup      = BeautifulSoup(full_html, 'html.parser')
    full_text = soup.get_text().lower()

    for sec in sections:
        title = sec.get('line', '').lower()
        if any(s in title for s in _SECTIONS_OF_INTEREST):
            sec_anchor = sec.get('anchor', '')
            header_tag = soup.find(id=sec_anchor)
            if not header_tag:
                continue
            paragraphs: list[str] = []
            for sibling in header_tag.parent.find_next_siblings(['p', 'h2', 'h3', 'h4']):
                if sibling.name in ('h2', 'h3', 'h4'):
                    break
                t = sibling.get_text(strip=True)
                if t:
                    paragraphs.append(t)
            safe_key = re.sub(r'[^a-z0-9]+', '_', title)[:30].strip('_')
            data[f'section_{safe_key}'] = ' '.join(paragraphs)[:500]

    for loco in ('thunniform', 'carangiform', 'subcarangiform', 'anguilliform',
                 'ostraciiform', 'labriform'):
        if loco in full_text:
            data['locomotion_type_mentioned'] = loco
            break

    m = re.search(r'([\d.]+)\s*(km/h|mph|knots?)', full_text)
    if m:
        val  = float(m.group(1))
        unit = m.group(2)
        if 'mph'  in unit: val *= 1.609
        elif 'knot' in unit: val *= 1.852
        data['max_speed_kmh'] = round(val, 1)

    data['diet_keywords'] = [w for w in _FOOD_WORDS if w in full_text]
    return data


async def fetch_reference_images(
    client: httpx.AsyncClient, genus: str, species: str, wiki_data: dict[str, Any]
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []

    if wiki_data.get('wiki_image_url'):
        images.append({
            'url':       wiki_data['wiki_image_url'],
            'thumbnail': wiki_data.get('wiki_thumbnail_url', ''),
            'source':    'wikipedia',
            'type':      'adult',
            'dims':      wiki_data.get('wiki_image_dims'),
        })

    try:
        r = await client.get(
            'https://www.fishbase.se/webservice/photos/FishPicsList.php',
            params={'Genus': genus, 'Species': species, 'type': ''},
            headers=HEADERS_HTML,
            timeout=15.0,
        )
        if r.status_code == 200 and r.text.strip():
            photo_soup = BeautifulSoup(r.text, 'xml')
            for pic_type in ('adult', 'larvae', 'juvenile'):
                pictures = photo_soup.find('pictures', attrs={'type': pic_type})
                if not pictures:
                    continue
                for actual, thumb in zip(
                    pictures.find_all('actual'),
                    pictures.find_all('thumbnail'),
                ):
                    url = actual.text.strip()
                    if url:
                        images.append({
                            'url':       url,
                            'thumbnail': thumb.text.strip() if thumb else '',
                            'source':    'fishbase',
                            'type':      pic_type,
                            'dims':      None,
                        })
    except Exception as exc:
        log.warning(f'FishBase photo fetch failed: {exc}')

    return images


# ---------------------------------------------------------------------------
# Coloration / pattern / scale-type extraction
#
# These power the per-species shader differences in fish_shader.py — they all
# fall back to safe defaults when the source text is sparse so the renderer
# always has something to work with.
# ---------------------------------------------------------------------------

# Words searched in coloration text. Order matters for multi-word matches
# (e.g. "silver-white" must come before "silver").
_COLOR_WORDS = (
    'silver-white', 'dark blue', 'pale blue', 'blue-green', 'olive-green',
    'silver', 'blue', 'green', 'olive', 'gold', 'golden', 'yellow', 'orange',
    'red', 'brown', 'black', 'white', 'grey', 'gray', 'pink', 'purple',
    'iridescent',
)

_FIN_KEYWORDS = ('caudal', 'dorsal', 'anal', 'pectoral', 'pelvic')

_PATTERN_KEYWORDS = {
    'spotted':            ('spotted', 'spots', 'speckled', 'ocellated'),
    'striped_horizontal': ('striped', 'stripes', 'longitudinal stripe'),
    'banded_vertical':    ('banded', 'bars', 'vertical bar', 'cross-band'),
    'mottled':            ('mottled', 'marbled', 'blotched', 'reticulated'),
}

_SCALE_TYPE_BY_FAMILY = {
    # Cycloid (smooth-edged round scales) — typical soft-rayed teleosts.
    'scombridae':     'cycloid',
    'salmonidae':     'cycloid',
    'clupeidae':      'cycloid',
    'cyprinidae':     'cycloid',
    'gadidae':        'cycloid',
    'esocidae':       'cycloid',
    # Ctenoid (toothed-edge scales) — most spiny-rayed perciforms.
    'percidae':       'ctenoid',
    'serranidae':     'ctenoid',
    'lutjanidae':     'ctenoid',
    'sparidae':       'ctenoid',
    'centrarchidae':  'ctenoid',
    'sciaenidae':     'ctenoid',
    'cichlidae':      'ctenoid',
    'pomacentridae':  'ctenoid',  # damsels / clownfish
    'labridae':       'ctenoid',
    # Ganoid (rhomboid armoured scales) — primitive ray-finned fish.
    'lepisosteidae':  'ganoid',
    'acipenseridae':  'ganoid',
    'polypteridae':   'ganoid',
    # Placoid (dermal denticles) — cartilaginous fish.
    'carcharhinidae': 'placoid',
    'lamnidae':       'placoid',
    'sphyrnidae':     'placoid',
    'rajidae':        'placoid',
    # Smooth / scaleless / minute embedded scales.
    'anguillidae':    'smooth',
    'muraenidae':     'smooth',
    'congridae':      'smooth',
    'ophichthidae':   'smooth',
    'ictaluridae':    'smooth',  # catfish
    'siluridae':      'smooth',
}


def _find_first_color(text: str) -> str | None:
    """Return the colour keyword that appears earliest in `text` (by position
    in the input), or None. Multi-word colours like 'silver-white' are
    preferred when they overlap a single-word match at the same position."""
    if not text:
        return None
    lt = text.lower()
    best_pos: int | None = None
    best_word: str | None = None
    for word in _COLOR_WORDS:
        idx = lt.find(word)
        if idx < 0:
            continue
        if best_pos is None or idx < best_pos:
            best_pos = idx
            best_word = word
        elif idx == best_pos and len(word) > len(best_word or ''):
            best_word = word
    return best_word


def _split_ventral_dorsal(text: str) -> tuple[str | None, str | None]:
    """Best-effort split of a coloration description into (dorsal, ventral) colors.

    Looks for prepositional cues like "X above" / "Y below" or "dark X / light Y".
    Falls back to (first_color, None).
    """
    if not text:
        return None, None
    lt = text.lower()

    dorsal = None
    ventral = None

    # "<color> above ... <color> below"
    m_above = re.search(r'([a-z\-]+)\s+(?:above|on\s+the\s+back|dorsally|on\s+top)', lt)
    m_below = re.search(r'([a-z\-]+)\s+(?:below|on\s+the\s+belly|ventrally|underneath)', lt)
    if m_above:
        dorsal = _find_first_color(m_above.group(1)) or _find_first_color(
            lt[max(0, m_above.start() - 30):m_above.end()]
        )
    if m_below:
        ventral = _find_first_color(m_below.group(1)) or _find_first_color(
            lt[max(0, m_below.start() - 30):m_below.end()]
        )

    if not dorsal:
        dorsal = _find_first_color(lt)

    return dorsal, ventral


def _detect_pattern(text: str) -> tuple[str, str | None]:
    """Return (pattern_enum, accent_color)."""
    if not text:
        return 'none', None
    lt = text.lower()
    for pattern, kws in _PATTERN_KEYWORDS.items():
        for kw in kws:
            if kw in lt:
                # Pattern color = colour word in same sentence as the pattern keyword.
                idx = lt.find(kw)
                window = lt[max(0, idx - 60): idx + 60]
                accent = _find_first_color(window)
                return pattern, accent
    return 'none', None


def _extract_fin_colors(text: str) -> dict[str, str | None]:
    """Extract a per-fin color from descriptions like 'yellow caudal finlets',
    'black-tipped pectoral fins', 'second dorsal fin yellow', etc.

    Wikipedia scrapes occasionally produce concatenated tokens like
    'yellowcaudalfinlets' (when reference markers are stripped between words),
    so we don't require a right-side word boundary on the fin name.
    """
    out: dict[str, str | None] = {k: None for k in _FIN_KEYWORDS}
    if not text:
        return out

    lt = text.lower()
    skip_substrings = {'anal': ('analy', 'anale')}
    for fin in _FIN_KEYWORDS:
        # No boundaries — Wikipedia scrapes can concatenate words
        # ('yellowcaudalfinlets'). False positives are filtered via
        # skip_substrings and the requirement that a colour word exists nearby.
        for m in re.finditer(fin, lt):
            s, e = m.start(), m.end()
            tail = lt[s: min(len(lt), s + 10)]
            if any(tail.startswith(skip) for skip in skip_substrings.get(fin, ())):
                continue
            # Try a tight window first (just before the fin keyword) so
            # 'yellowcaudal' / 'yellow caudal fin' / 'caudal fin is yellow'
            # all bind to the colour right next to the fin name, not whatever
            # other colour appeared earlier in the same sentence.
            tight = lt[max(0, s - 25): min(len(lt), e + 25)]
            color = _find_first_color(tight)
            if not color:
                wide = lt[max(0, s - 80): min(len(lt), e + 80)]
                color = _find_first_color(wide)
            if color:
                out[fin] = color
                break
    return out


def _has_finlets(text: str) -> bool:
    return bool(text) and 'finlet' in text.lower()


def _is_iridescent(text: str) -> bool:
    if not text:
        return False
    lt = text.lower()
    return any(w in lt for w in (
        'iridescent', 'metallic sheen', 'shimmer', 'shimmering',
        'opalescent', 'rainbow sheen',
    ))


def _detect_scale_type(text: str, family: str, taxonomic_class: str) -> str:
    """Best-effort scale type. Tries explicit text mentions first, then a
    family lookup, then class-level fallback (placoid for chondrichthyans)."""
    lt = (text or '').lower()
    for stype in ('placoid', 'ctenoid', 'cycloid', 'ganoid'):
        if stype in lt:
            return stype
    family_lower = family.lower()
    if family_lower.endswith('inae'):
        family_lower = family_lower[:-4] + 'idae'
    if family_lower in _SCALE_TYPE_BY_FAMILY:
        return _SCALE_TYPE_BY_FAMILY[family_lower]
    if taxonomic_class.lower() in ('elasmobranchii', 'chondrichthyes'):
        return 'placoid'
    return 'cycloid'


def build_coloration_details(
    fishbase: dict[str, Any], wiki: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate the colour/pattern fields the shader consumes."""
    text = ' '.join([
        fishbase.get('coloration_raw') or '',
        wiki.get('section_description', ''),
        wiki.get('intro', ''),
    ]).strip()

    dorsal, ventral = _split_ventral_dorsal(text)
    pattern, pattern_color = _detect_pattern(text)
    lateral_match = re.search(
        r'lateral\s+line[^.]{0,80}?\b([a-z\-]+)\b', text.lower()
    )
    lateral_color = _find_first_color(lateral_match.group(0)) if lateral_match else None

    return {
        'dorsal_color':       dorsal,
        'ventral_color':      ventral,
        'lateral_line_color': lateral_color,
        'iridescent':         _is_iridescent(text),
        'pattern':            pattern,
        'pattern_color':      pattern_color,
    }


# ---------------------------------------------------------------------------
# Locomotion inference
# ---------------------------------------------------------------------------

def infer_locomotion(family: str, order: str) -> dict[str, Any]:
    family_lower = family.lower()
    if family_lower.endswith('inae'):
        family_lower = family_lower[:-4] + 'idae'
    loco_type, body_undulation = (
        _LOCO_BY_FAMILY.get(family_lower)
        or _LOCO_BY_ORDER.get(order.lower())
        or ('subcarangiform', 0.50)
    )
    return {
        'type':             loco_type,
        'description':      _LOCO_DESCRIPTIONS.get(loco_type, ''),
        'body_undulation':  body_undulation,
        'tail_shape':       _TAIL_SHAPES.get(loco_type, 'forked'),
        'pectoral_fin_role': 'primary' if loco_type == 'labriform' else 'steering',
    }


# ---------------------------------------------------------------------------
# Blender parameter derivation
# ---------------------------------------------------------------------------

def _body_depth_ratio(fishbase: dict[str, Any]) -> float:
    desc = (fishbase.get('body_shape_desc', '') + ' ' + fishbase.get('common_name', '')).lower()
    if any(w in desc for w in ('elongat', 'eel-like', 'ribbon', 'snake')):
        return 0.12
    if any(w in desc for w in ('fusiform', 'torpedo', 'streamlin')):
        return 0.22
    if any(w in desc for w in ('compress', 'deep bod', 'disc', 'oval')):
        return 0.52
    if any(w in desc for w in ('depress', 'flat', 'ray-like', 'benthic')):
        return 0.20
    return 0.30


def _feeding_style(fishbase: dict[str, Any], wiki: dict[str, Any]) -> tuple[str, str]:
    trophic = fishbase.get('trophic_level') or 3.0
    teeth   = fishbase.get('teeth_desc', '').lower()
    diet    = set(wiki.get('diet_keywords', []))

    eats_active  = bool(diet & _ACTIVE_PREY)
    eats_passive = bool(diet & _PASSIVE_PREY) and not eats_active

    if trophic >= 4.5 or any(w in teeth for w in ('large', 'fang', 'canine', 'recurved', 'sharp')):
        return 'ram-strike', 'large'
    if eats_active and trophic >= 3.5:
        return 'ram-strike', 'large'
    if eats_active:
        return 'suction-strike', 'medium'
    if eats_passive or trophic <= 2.2:
        return 'filter', 'wide'
    return 'suction', 'small'


def _animation_clips(feeding_style: str, loco_type: str, dangerous: bool) -> list[dict]:
    clips = [
        {'name': 'idle_swim',  'description': 'Looping swim cycle',   'frames': 48},
        {'name': 'turn_left',  'description': 'Banking left turn',    'frames': 24},
        {'name': 'turn_right', 'description': 'Banking right turn',   'frames': 24},
        {'name': 'dive',       'description': 'Downward dive',        'frames': 30},
        {'name': 'surface',    'description': 'Rising to surface',    'frames': 30},
        {'name': 'startle',    'description': 'Escape-response burst','frames': 12},
    ]
    if feeding_style == 'ram-strike':
        clips += [
            {'name': 'attack_strike', 'description': 'Burst lunge at prey',  'frames': 20},
            {'name': 'feeding_bite',  'description': 'Mouth open/close bite', 'frames': 16},
        ]
    elif feeding_style == 'suction-strike':
        clips.append({'name': 'suction_feed', 'description': 'Rapid suction strike', 'frames': 12})
    elif feeding_style == 'filter':
        clips.append({'name': 'filter_swim', 'description': 'Open-mouth filter pass', 'frames': 60})

    if dangerous:
        clips.append({'name': 'threat_display', 'description': 'Threat/warning posture', 'frames': 30})
    if loco_type == 'anguilliform':
        clips.append({'name': 'burrow', 'description': 'Burrowing into substrate', 'frames': 40})

    return clips


def _fishsim_params(loco: dict[str, Any], fishbase: dict[str, Any]) -> dict[str, Any]:
    base      = _FISHSIM_BY_LOCO.get(loco['type'], _FISHSIM_BY_LOCO['subcarangiform']).copy()
    length_cm = fishbase.get('max_length_cm') or 30.0
    effort    = round(min(2.0, max(0.3, length_cm / 100.0)), 2)
    max_steer = round(loco['body_undulation'] * 60 + 10, 1)
    return {
        **base,
        'effort':             effort,
        'max_steering_angle': max_steer,
        'hover_mode':         loco['type'] in ('labriform', 'ostraciiform'),
        'note': (
            'Import these values into the FishSim addon panel '
            '(extensions.blender.org/add-ons/fishsim/) after adding a fish rig.'
        ),
    }


def build_blender_params(
    fishbase: dict[str, Any], wiki: dict[str, Any], loco: dict[str, Any]
) -> dict[str, Any]:
    feeding_style, mouth_gape = _feeding_style(fishbase, wiki)
    loco_type  = loco['type']
    dangerous  = fishbase.get('dangerous', False)
    undulation = loco['body_undulation']
    spine_bones = 10 if undulation >= 0.70 else (8 if undulation >= 0.40 else 6)

    return {
        'body_depth_ratio':             _body_depth_ratio(fishbase),
        'body_undulation':              undulation,
        'tail_shape':                   loco['tail_shape'],
        'pectoral_fin_role':            loco['pectoral_fin_role'],
        'feeding_style':                feeding_style,
        'mouth_gape':                   mouth_gape,
        'is_dangerous':                 dangerous,
        'recommended_spine_bone_count': spine_bones,
        'animation_clips':              _animation_clips(feeding_style, loco_type, dangerous),
        'fishsim_params':               _fishsim_params(loco, fishbase),
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

async def research_species(species_name: str) -> dict[str, Any]:
    """Research a fish species and return the full structured output dict.

    This is the single entry point used by both the standby HTTP server
    and the batch actor mode.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        log.info(f'Resolving scientific name for: {species_name!r}')
        wiki_page_title, scientific_name = await resolve_scientific_name(client, species_name)
        log.info(f'Scientific name: {scientific_name!r} (page: {wiki_page_title!r})')

        genus, species_epithet = scientific_name.split()[0], scientific_name.split()[1]
        fishbase_url = f'https://www.fishbase.se/summary/{genus}-{species_epithet}.html'

        log.info('Scraping FishBase and Wikipedia concurrently...')
        fishbase_data, wiki_data = await asyncio.gather(
            scrape_fishbase(client, scientific_name),
            scrape_wikipedia(client, wiki_page_title),
        )
        reference_images = await fetch_reference_images(
            client, genus, species_epithet, wiki_data
        )

    fishbase_data['fishbase_url'] = fishbase_url

    family = fishbase_data.get('family', '')
    order  = fishbase_data.get('order', '')
    loco   = infer_locomotion(family, order)

    loco_mentioned = wiki_data.pop('locomotion_type_mentioned', None)
    if loco_mentioned:
        log.info(f'Wikipedia identifies locomotion as: {loco_mentioned}')
        loco['type']           = loco_mentioned
        loco['body_undulation'] = _LOCO_TYPE_TO_UNDULATION.get(loco_mentioned, loco['body_undulation'])
        loco['tail_shape']     = _TAIL_SHAPES.get(loco_mentioned, loco['tail_shape'])

    blender_params = build_blender_params(fishbase_data, wiki_data, loco)

    coloration_details = build_coloration_details(fishbase_data, wiki_data)
    color_text = ' '.join([
        fishbase_data.get('coloration_raw') or '',
        wiki_data.get('section_description', ''),
        wiki_data.get('intro', ''),
    ])
    fin_colors = _extract_fin_colors(color_text)
    has_finlets = _has_finlets(color_text)
    scale_type = _detect_scale_type(
        color_text, family, fishbase_data.get('class_', '')
    )

    parts = scientific_name.split()
    return {
        'species': {
            'input_name':       species_name,
            'scientific_name':  scientific_name,
            'genus':            parts[0] if parts else '',
            'specific_epithet': parts[1] if len(parts) > 1 else '',
            'common_name':      fishbase_data.get('common_name', species_name),
            'family':           family,
            'order':            order,
            'class':            fishbase_data.get('class_', ''),
        },
        'morphology': {
            'max_length_cm':          fishbase_data.get('max_length_cm'),
            'max_weight_kg':          fishbase_data.get('max_weight_kg'),
            'depth_range_m': {
                'min': fishbase_data.get('depth_min_m'),
                'max': fishbase_data.get('depth_max_m'),
            } if fishbase_data.get('depth_min_m') else None,
            'body_shape_description': fishbase_data.get('body_shape_desc', ''),
            'coloration_description': (
                fishbase_data.get('coloration_raw')
                or wiki_data.get('section_description', '')[:300]
            ),
            'teeth_description': fishbase_data.get('teeth_desc', ''),
            'fins': {
                'dorsal_spines': fishbase_data.get('dorsal_spines'),
                'dorsal_rays':   fishbase_data.get('dorsal_rays'),
                'anal_spines':   fishbase_data.get('anal_spines'),
                'anal_rays':     fishbase_data.get('anal_rays'),
            },
            'fin_colors':         fin_colors,
            'has_finlets':        has_finlets,
            'scale_type':         scale_type,
            'coloration_details': coloration_details,
            'trophic_level':      fishbase_data.get('trophic_level'),
            'is_dangerous':       fishbase_data.get('dangerous', False),
        },
        'locomotion': loco,
        'behavior': {
            'max_speed_kmh': wiki_data.get('max_speed_kmh'),
            'diet_keywords': wiki_data.get('diet_keywords', []),
            'description':   wiki_data.get('intro', ''),
            'sections': {
                k: v for k, v in wiki_data.items() if k.startswith('section_')
            },
        },
        'blender_params':    blender_params,
        'reference_images':  reference_images,
        'sources': {
            'fishbase_url':  fishbase_url,
            'wikipedia_url': wiki_data.get('wikipedia_url', ''),
        },
    }
