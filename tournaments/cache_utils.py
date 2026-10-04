"""Tiny view-cache decorator for the anonymous public views.

Behaves like django.views.decorators.cache.cache_page, but also stamps an
X-Cache: HIT/MISS response header so nginx access logs can report cache hit
ratios for Phase 5 (production monitoring) without adding new infrastructure.

The cache key contains:
  * the full path,
  * the visitor's explicit language choice (cookie) — without one, the public
    views use the club's language, which is the same for every visitor,
  * the page version bumped by ``signals.invalidate_public_cache`` whenever a
    tournament, division, match … changes, so results show up immediately.

Browsers get ``Cache-Control: max-age=<timeout>`` so they never keep a page
longer than the server-side cache does.
"""
import functools

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.cache import patch_cache_control

LANGUAGE_COOKIE = getattr(settings, 'LANGUAGE_COOKIE_NAME', 'django_language')


def page_version_key(page, tournament_id=None):
    """Cache key holding the current version of a public page (see signals.invalidate_public_cache)."""
    return f'public-page-version:{page}:{tournament_id or "all"}'


def cache_page_with_status(timeout, key_prefix='pubcache', page=None):
    """Cache a view's 200 responses in the default cache for `timeout` seconds.

    ``page`` ('landing', 'tournament' or 'schedule') ties the entry to the
    version counter that is bumped when the underlying data changes.
    """
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            version = cache.get(page_version_key(page, kwargs.get('pk')), 1) if page else 1
            lang = request.COOKIES.get(LANGUAGE_COOKIE, '-')
            cache_key = f'{key_prefix}:v{version}:{lang}:{request.get_full_path()}'
            cached = cache.get(cache_key)
            if cached is not None:
                response = HttpResponse(
                    cached['content'], content_type=cached['content_type'], status=cached['status']
                )
                response['X-Cache'] = 'HIT'
            else:
                response = view_func(request, *args, **kwargs)
                if response.status_code == 200:
                    cache.set(cache_key, {
                        'content': response.content,
                        'content_type': response.get('Content-Type', 'text/html'),
                        'status': response.status_code,
                    }, timeout)
                response['X-Cache'] = 'MISS'
            if response.status_code == 200:
                patch_cache_control(response, max_age=timeout)
            return response
        return wrapper
    return decorator
