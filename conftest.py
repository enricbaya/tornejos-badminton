import pytest


@pytest.fixture(autouse=True)
def _reset_active_language():
    """Each test starts in the default language.

    LocaleMiddleware activates the request language on the test thread and does not
    reset it, so a test that requests a Catalan page would otherwise leak Catalan
    into the following tests.
    """
    from django.conf import settings
    from django.utils import translation
    translation.activate(settings.LANGUAGE_CODE)
    yield
    translation.deactivate()
