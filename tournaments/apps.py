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
        from django.db.models.signals import post_migrate
        post_migrate.connect(translate_permission_names, dispatch_uid='club_translate_permission_names')


def translate_permission_names(sender=None, using='default', **kwargs):
    """Store the default permission names ("Can add …") in the site language.

    Django saves them in English from the untranslated model names, so the
    permission lists in the admin would otherwise stay half in English.
    Runs after every migrate (the Docker entrypoint migrates on start-up).
    """
    from django.conf import settings
    from django.contrib.auth.models import Permission
    from django.utils import translation
    from django.utils.translation import gettext

    verbs = {'add': 'Can add %(model)s', 'change': 'Can change %(model)s',
             'delete': 'Can delete %(model)s', 'view': 'Can view %(model)s'}
    with translation.override(settings.LANGUAGE_CODE):
        for perm in Permission.objects.using(using).select_related('content_type'):
            action = perm.codename.split('_', 1)[0]
            model = perm.content_type.model_class()
            if action not in verbs or model is None or perm.codename != f'{action}_{model._meta.model_name}':
                continue
            name = (gettext(verbs[action]) % {'model': str(model._meta.verbose_name).lower()})[:255]
            if perm.name != name:
                perm.name = name
                perm.save(update_fields=['name'])


class AxesConfig(__import__('axes.apps', fromlist=['AppConfig']).AppConfig):
    """django-axes with a translatable admin section name."""
    verbose_name = _('Login security')
