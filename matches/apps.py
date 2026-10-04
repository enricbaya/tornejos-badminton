from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MatchesConfig(AppConfig):
    """Configure Django settings for the matches app."""

    name = 'matches'
    verbose_name = _('Matches (legacy, unused)')
