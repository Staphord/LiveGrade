"""GitHub Release metadata and OS detection helpers for desktop downloads."""
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.cache import cache

GITHUB_RELEASES_API = 'https://api.github.com/repos/Staphord/LiveGrade/releases/latest'
GITHUB_RELEASES_PAGE = 'https://github.com/Staphord/LiveGrade/releases/latest'
RELEASE_CACHE_KEY = 'desktop_latest_release:v1'
RELEASE_STALE_KEY = 'desktop_latest_release:stale:v1'
RELEASE_CACHE_TTL = 900
RELEASE_STALE_TTL = 86400

PLATFORM_META = {
    'mac': {
        'label': 'macOS',
        'icon': 'bi-apple',
        'subtitle': 'Universal (Apple Silicon & Intel)',
    },
    'win': {
        'label': 'Windows',
        'icon': 'bi-windows',
        'subtitle': 'Windows 10 & 11 (64-bit x64)',
    },
    'linux': {
        'label': 'Linux',
        'icon': 'bi-ubuntu',
        'subtitle': '64-bit x64 (AppImage & .deb)',
    },
}


def detect_os_from_user_agent(user_agent):
    """Infer desktop platform key ('mac', 'win', or 'linux') from a User-Agent string."""
    ua = (user_agent or '').lower()
    if 'windows' in ua:
        return 'win'
    if 'macintosh' in ua or 'mac os' in ua or 'darwin' in ua:
        return 'mac'
    if 'linux' in ua and 'android' not in ua:
        return 'linux'
    return 'mac'


def format_bytes(num_bytes):
    """Format a byte count as a human-readable megabyte string."""
    if not isinstance(num_bytes, (int, float)) or num_bytes <= 0:
        return ''
    mb = num_bytes / (1024 * 1024)
    if mb >= 1:
        return f'{mb:.1f} MB'
    kb = num_bytes / 1024
    return f'{kb:.0f} KB'


def _is_safe_https_url(url):
    try:
        parsed = urlsplit(str(url or ''))
    except ValueError:
        return False
    return parsed.scheme == 'https' and bool(parsed.netloc)


def _match_asset(assets, suffixes, release_url, ext_label):
    """Find the first release asset matching any suffix, or return a safe fallback."""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get('name') or '')
        lower = name.lower()
        if lower.endswith('.blockmap'):
            continue
        if any(lower.endswith(suffix.lower()) for suffix in suffixes):
            url = asset.get('browser_download_url') or ''
            if _is_safe_https_url(url):
                return {
                    'name': name,
                    'url': url,
                    'size': format_bytes(asset.get('size')),
                    'ext': ext_label,
                    'available': True,
                }
    return {
        'name': '',
        'url': release_url,
        'size': '',
        'ext': ext_label,
        'available': False,
    }


DEFAULT_RELEASE_NOTES = [
    'Dedicated desktop window for LiveGrade sessions and workspaces.',
    'Automatic secondary-display projector window support.',
    'Secure sign-in through your browser with automatic updates.',
]


def _parse_release_notes(body):
    """Extract clean plain-text note lines from a GitHub release body."""
    lines = []
    for raw_line in str(body or '').splitlines():
        cleaned = raw_line.strip().lstrip('#*-• ').strip()
        if cleaned:
            lines.append(cleaned)
    return lines or list(DEFAULT_RELEASE_NOTES)


def parse_release_payload(payload):
    """Normalize a GitHub Releases API JSON payload into template-ready metadata."""
    if not isinstance(payload, dict):
        payload = {}

    raw_tag = str(payload.get('tag_name') or '').strip()
    version = raw_tag.lstrip('v') or '1.0.0'
    release_url = payload.get('html_url') or GITHUB_RELEASES_PAGE
    if not _is_safe_https_url(release_url):
        release_url = GITHUB_RELEASES_PAGE

    published_raw = str(payload.get('published_at') or '')
    published_date = published_raw[:10] if len(published_raw) >= 10 else ''
    raw_assets = payload.get('assets') if isinstance(payload.get('assets'), list) else []
    notes = _parse_release_notes(payload.get('body'))

    assets = {
        'mac_dmg': _match_asset(raw_assets, ['universal.dmg', '.dmg'], release_url, '.dmg'),
        'mac_zip': _match_asset(raw_assets, ['universal-mac.zip', '-mac.zip'], release_url, '.zip'),
        'win_exe': _match_asset(raw_assets, ['.exe'], release_url, '.exe'),
        'linux_appimage': _match_asset(raw_assets, ['.AppImage'], release_url, '.AppImage'),
        'linux_deb': _match_asset(raw_assets, ['.deb'], release_url, '.deb'),
    }

    return {
        'version': version,
        'tag_name': raw_tag or f'v{version}',
        'release_url': release_url,
        'published_date': published_date,
        'notes': notes,
        'assets': assets,
        'has_assets': any(item['available'] for item in assets.values()),
    }


def fetch_latest_release_payload():
    """Fetch the latest release JSON from GitHub."""
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'LiveGrade-Web',
    }
    token = getattr(settings, 'GITHUB_RELEASES_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    response = requests.get(GITHUB_RELEASES_API, headers=headers, timeout=4)
    response.raise_for_status()
    return response.json()


def get_latest_desktop_release():
    """Return cached desktop release metadata, falling back gracefully on network errors."""
    cached = cache.get(RELEASE_CACHE_KEY)
    if isinstance(cached, dict):
        return cached

    try:
        payload = fetch_latest_release_payload()
        parsed = parse_release_payload(payload)
        cache.set(RELEASE_CACHE_KEY, parsed, timeout=RELEASE_CACHE_TTL)
        cache.set(RELEASE_STALE_KEY, parsed, timeout=RELEASE_STALE_TTL)
        return parsed
    except (requests.RequestException, ValueError, OSError):
        stale = cache.get(RELEASE_STALE_KEY)
        if isinstance(stale, dict):
            return stale
        return parse_release_payload({})


def _featured_asset_for(platform_key, release):
    assets = release['assets']
    if platform_key == 'win':
        primary = assets['win_exe']
        return {
            'url': primary['url'],
            'ext': '.exe',
            'size': primary['size'],
            'subtitle': 'Windows 10 and 11 (64-bit)',
            'secondary_url': '',
            'secondary_label': '',
            'available': primary['available'],
        }
    if platform_key == 'linux':
        primary = assets['linux_appimage']
        secondary = assets['linux_deb']
        return {
            'url': primary['url'],
            'ext': '.AppImage',
            'size': primary['size'],
            'subtitle': '64-bit Linux',
            'secondary_url': secondary['url'],
            'secondary_label': 'Debian (.deb)',
            'available': primary['available'],
        }
    primary = assets['mac_dmg']
    secondary = assets['mac_zip']
    return {
        'url': primary['url'],
        'ext': '.dmg',
        'size': primary['size'],
        'subtitle': 'Apple Silicon and Intel',
        'secondary_url': secondary['url'],
        'secondary_label': 'Zip archive',
        'available': primary['available'],
    }


def build_download_context(user_agent):
    """Build template context for the desktop download page."""
    detected_os = detect_os_from_user_agent(user_agent)
    release = get_latest_desktop_release()
    meta = PLATFORM_META[detected_os]
    featured = _featured_asset_for(detected_os, release)
    return {
        'release': release,
        'assets': release['assets'],
        'detected_os': detected_os,
        'detected_label': meta['label'],
        'detected_icon': meta['icon'],
        'featured_asset': featured,
    }

