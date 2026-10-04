"""Court referee mode: one secret link (and QR code) per court.

A referee scans the QR code on the court sign and gets a phone page for *that*
court only: it proposes the next match that can start, lets them start it and
enter the final score, which frees the court. No user account is needed; the
link is signed with a per-tournament key that the judge table can regenerate
(invalidating every printed QR code) or switch off entirely.
"""
import hmac
import secrets
from hashlib import sha256

from django.conf import settings
from django.urls import reverse


def ensure_key(tournament):
    """Return the tournament's court access key, creating one if missing."""
    if not tournament.court_access_key:
        tournament.court_access_key = secrets.token_hex(16)
        tournament.save(update_fields=['court_access_key'])
    return tournament.court_access_key


def regenerate_key(tournament):
    """Replace the key: all existing court links/QR codes stop working."""
    tournament.court_access_key = secrets.token_hex(16)
    tournament.save(update_fields=['court_access_key'])
    return tournament.court_access_key


def court_signature(tournament, court):
    """Signature for (tournament, court). Requires the key to exist."""
    key = f'{settings.SECRET_KEY}:{tournament.court_access_key}'.encode()
    msg = f'court:{tournament.pk}:{int(court)}'.encode()
    return hmac.new(key, msg, sha256).hexdigest()[:20]


def signature_is_valid(tournament, court, sig):
    if not tournament.court_access_key or not sig:
        return False
    return hmac.compare_digest(court_signature(tournament, court), str(sig))


def court_count(tournament):
    """Highest number of courts over the tournament days (4 if no days yet)."""
    return max((d.court_count for d in tournament.days.all()), default=4)


def court_path(tournament, court):
    ensure_key(tournament)
    return reverse('court_panel', args=[tournament.pk, court, court_signature(tournament, court)])


def court_url(request, tournament, court):
    """Absolute court link. PUBLIC_BASE_URL (e.g. https://tornejos.example.org) wins over the
    request host, so QR codes always point to the public https address behind a proxy/tunnel."""
    base = getattr(settings, 'PUBLIC_BASE_URL', '').rstrip('/')
    path = court_path(tournament, court)
    return f'{base}{path}' if base else request.build_absolute_uri(path)


def qr_svg(data, size_mm=None):
    """Return an inline SVG QR code for `data` (no external service involved)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    svg = img.to_string(encoding='unicode')
    # Make it scale with its container.
    svg = svg.replace('<svg ', '<svg preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block" ', 1)
    return svg
