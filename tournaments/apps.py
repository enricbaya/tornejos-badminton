from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class TournamentsConfig(AppConfig):
    """Configure Django settings for the tournaments app."""

    default = True  # this module also defines AxesConfig

    name = 'tournaments'
    verbose_name = _('Tournaments')

    def ready(self):
        """Import signal handlers when Django starts the app."""
        import tournaments.signals  # noqa


class AxesConfig(__import__('axes.apps', fromlist=['AppConfig']).AppConfig):
    """django-axes with a translatable admin section name."""
    verbose_name = _('Login security')
