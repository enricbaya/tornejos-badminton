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


# ---------------------------------------------------------------------------
# Player names: link to the public player page + school / club
# ---------------------------------------------------------------------------

from django.urls import reverse  # noqa: E402
from django.utils.html import format_html, format_html_join  # noqa: E402
from django.utils.safestring import mark_safe  # noqa: E402


@register.filter
def team_school(team):
    """Small school/club label of a team ('' when none is set)."""
    label = getattr(team, 'school_label', '') if team else ''
    return format_html('<span class="school-tag">{}</span>', label) if label else ''


@register.filter
def team_html(team, tournament_pk):
    """Team name with each player linked to their public page, plus the school label."""
    if not team:
        return ''
    players = team.players
    if not players or not tournament_pk:
        return format_html('{}{}', team.name, team_school(team))
    links = format_html_join(
        mark_safe(' &amp; '), '<a class="player-link" href="{}">{}</a>',
        ((reverse('public_player', args=[tournament_pk, p.pk]), p.name) for p in players))
    return format_html('{}{}', links, team_school(team))


@register.filter
def signed(value):
    """+3 / 0 / -2"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return value
    return f'+{v}' if v > 0 else str(v)


@register.filter
def stage_label(stage, division):
    """Readable knockout stage from medals.rank_teams (0 = did not reach the knockout)."""
    from django.db.models import Max
    from django.utils.translation import gettext as _
    if not stage:
        return _("Group stage") if (division.has_groups or division.tournament_type == 'group') else '–'
    qs = division.matches.filter(phase='playoff') if division.tournament_type == 'playoff' else division.matches.all()
    final_round = qs.aggregate(m=Max('match_round'))['m'] or 0
    left = final_round + 1 - stage  # 0 champion, 1 final, 2 semi-final, 3 quarter-final …
    names = {0: _("Champion"), 1: _("Final"), 2: _("Semi-final"), 3: _("Quarter-final")}
    return names.get(left, _("Round %(n)s") % {'n': stage})


@register.filter
def add_minutes(dt, minutes):
    from datetime import timedelta
    try:
        return dt + timedelta(minutes=int(minutes))
    except (TypeError, ValueError):
        return dt


@register.filter
def has_pending(matches):
    return any(getattr(m, 'status', '') == 'pending' for m in matches)


@register.filter
def hm(minutes):
    """205 → '3 h 25 min'"""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return '–'
    return f'{m // 60} h {m % 60:02d} min' if m >= 60 else f'{m} min'


@register.filter
def ordinal(n):
    try:
        return _ordinal(n)
    except (TypeError, ValueError):
        return n
