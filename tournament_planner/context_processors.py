"""Template context shared by every page."""
import hashlib
from pathlib import Path

from django.conf import settings

_VERSION = None


def _static_version():
    """Short hash of the club's own static files: changes whenever they change, so browsers
    (and the service worker) fetch the new version instead of an old cached copy."""
    global _VERSION
    if _VERSION is None:
        h = hashlib.sha1()
        for d in getattr(settings, 'STATICFILES_DIRS', []):
            for rel in ('club/club.css', 'js/result_entry_modal.js'):
                p = Path(d) / rel
                if p.is_file():
                    h.update(p.read_bytes())
        _VERSION = h.hexdigest()[:10]
    return _VERSION


def static_version(request):
    return {'STATIC_VERSION': _static_version()}
