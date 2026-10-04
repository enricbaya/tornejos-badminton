import logging

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db.models.signals import post_save
from django.db.models.signals import post_delete, m2m_changed
from django.dispatch import receiver
from django.utils import translation
from django.core.cache import cache

from .models import Tournament, TournamentDay, Division, Match, DivisionSeed, MedalOverride
from .cache_utils import page_version_key

# Dedicated logger so admins can route auth events to a separate handler
# (rsyslog, fail2ban, SIEM, etc.) via the LOGGING dict in settings.py.
auth_log = logging.getLogger('btp.auth')

LANGUAGE_COOKIE_NAME = getattr(django_settings, 'LANGUAGE_COOKIE_NAME', 'django_language')
LANGUAGE_COOKIE_AGE = getattr(django_settings, 'LANGUAGE_COOKIE_AGE', None)
LANGUAGE_COOKIE_DOMAIN = getattr(django_settings, 'LANGUAGE_COOKIE_DOMAIN', None)
LANGUAGE_COOKIE_PATH = getattr(django_settings, 'LANGUAGE_COOKIE_PATH', '/')
LANGUAGE_COOKIE_SECURE = getattr(django_settings, 'LANGUAGE_COOKIE_SECURE', False)
LANGUAGE_COOKIE_HTTPONLY = getattr(django_settings, 'LANGUAGE_COOKIE_HTTPONLY', False)
LANGUAGE_COOKIE_SAMESITE = getattr(django_settings, 'LANGUAGE_COOKIE_SAMESITE', 'Lax')


def _client_ip(request):
    """Return the best-guess client IP, honouring X-Forwarded-For from nginx."""
    if request is None:
        return '-'
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '-')


@receiver(user_logged_in)
def _log_user_logged_in(sender, request, user, **kwargs):
    auth_log.info('login_ok user=%s ip=%s', user.get_username(), _client_ip(request))


@receiver(user_logged_out)
def _log_user_logged_out(sender, request, user, **kwargs):
    username = user.get_username() if user else '-'
    auth_log.info('logout    user=%s ip=%s', username, _client_ip(request))


@receiver(user_login_failed)
def _log_user_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get('username', '-')
    auth_log.warning('login_FAIL user=%s ip=%s', username, _client_ip(request))


@receiver(user_logged_in)
def set_language_on_login(sender, request, user, **kwargs):
    """Activate the club's preferred language after login and persist it via cookie."""
    # Only apply if the user hasn't already chosen a language via the switcher
    if LANGUAGE_COOKIE_NAME in request.COOKIES:
        return
    try:
        lang = user.profile.language
    except Exception:
        return
    if lang:
        translation.activate(lang)
        # Store on response — Django's LocaleMiddleware will pick this up,
        # but we also need to set it on the response object.
        # We attach it to the request so the login view's response can set it.
        request._set_language_cookie = lang


@receiver(post_save, sender=django_settings.AUTH_USER_MODEL)
def _create_user_profile(sender, instance, created, **kwargs):
    """Auto-create a UserProfile when a new user is saved for the first time."""
    if not created:
        return
    from .models import UserProfile
    tier = 'unlimited' if instance.is_superuser else 'small'
    UserProfile.objects.get_or_create(user=instance, defaults={'tier': tier})


def invalidate_public_cache(tournament_id):
    """Advance versions so public pages immediately bypass stale entries."""
    for name, page_tournament_id in (
        ('landing', None),
        ('tournament', tournament_id),
        ('schedule', tournament_id),
    ):
        key = page_version_key(name, page_tournament_id)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 2, None)


@receiver([post_save, post_delete], sender=Tournament)
def invalidate_for_tournament(sender, instance, **kwargs):
    invalidate_public_cache(instance.pk)


@receiver([post_save, post_delete], sender=TournamentDay)
@receiver([post_save, post_delete], sender=Division)
def invalidate_for_tournament_child(sender, instance, **kwargs):
    invalidate_public_cache(instance.tournament_id)


@receiver([post_save, post_delete], sender=Match)
@receiver([post_save, post_delete], sender=DivisionSeed)
@receiver([post_save, post_delete], sender=MedalOverride)
def invalidate_for_division_child(sender, instance, **kwargs):
    invalidate_public_cache(instance.division.tournament_id)


@receiver(m2m_changed, sender=Division.teams.through)
def invalidate_for_division_teams(sender, instance, action, **kwargs):
    if action in ('post_add', 'post_remove', 'post_clear'):
        invalidate_public_cache(instance.tournament_id)
