from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PlayersConfig(AppConfig):
    """Configure Django settings for the players app."""

    name = 'players'
    verbose_name = _('Players and teams')
