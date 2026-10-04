from django import template

register = template.Library()


@register.filter
def split(value, sep):
    """Split a string by *sep* and return a list."""
    if value:
        return value.split(sep)
    return [value]


@register.filter
def dict_get(d, key):
    """Look up *key* in dict *d*. Returns '' if not found."""
    if isinstance(d, dict):
        return d.get(key, '')
    return ''


@register.filter
def unix_ts(dt):
    """Convert a datetime to a Unix timestamp integer for JS timers."""
    if dt is None:
        return ''
    try:
        return int(dt.timestamp())
    except Exception:
        return ''


_POS_GROUP_RE = None


def _ordinal(n):
    """Short ordinal in the active language (1r/2n/3r/4t in Catalan, 1st/2nd in English)."""
    from django.utils.translation import get_language
    lang = (get_language() or 'en')[:2]
    n = int(n)
    if lang == 'ca':
        return {1: '1r', 2: '2n', 3: '3r', 4: '4t'}.get(n, f'{n}è')
    if lang == 'da':
        return f'{n}.'
    if 10 <= n % 100 <= 20:
        return f'{n}th'
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


@register.filter
def bracket_label_i18n(label):
    """Translate stored bracket placeholder labels for display.

    Labels are stored in a fixed (Danish) internal format because the scheduler
    parses them: "Nr.1 gr.2", "... (fri)" for byes and "V-kamp #12" for
    "winner of match 12". This filter renders them in the active language.
    """
    import re
    from django.utils.translation import gettext as _
    if not label:
        return label
    text = str(label)
    text = re.sub(
        r'Nr\.(\d+) gr\.(\d+)',
        lambda m: _("%(ordinal)s of group %(group)s") % {'ordinal': _ordinal(m.group(1)), 'group': m.group(2)},
        text,
    )
    text = text.replace('(fri)', _("(bye)"))
    text = re.sub(r'V-kamp #(\S+)', lambda m: _("Winner of #%(n)s") % {'n': m.group(1)}, text)
    return text
