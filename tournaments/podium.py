"""Matches that can end on the podium (quarter-finals, semi-finals, 3rd place, final and
final round-robin). They are highlighted on the loudspeaker panel, the court lists, the
big screen and the court referee page so they do not go unnoticed."""
from django.db.models import Max
from django.utils.translation import gettext as _

from .final_phase import section_label

# code -> (icon, short label). The order is also the importance order.
_STAGES = {
    'final': ('🏆', lambda: _("Final")),
    'third': ('🥉', lambda: _("Match for 3rd place")),
    'semi': ('⭐', lambda: _("Semi-final")),
    'quarter': ('🎖', lambda: _("Quarter-final")),
    'league': ('🏅', lambda: _("Final round-robin")),
}


def _ko_queryset(division):
    if division.tournament_type == 'tree':
        return division.matches.filter(bracket_slot__isnull=False)
    if division.tournament_type == 'playoff':
        return division.matches.filter(phase='playoff', bracket_slot__isnull=False)
    return None


def _last_round(division, section, cache):
    key = (division.pk, section or '')
    if key not in cache:
        qs = _ko_queryset(division)
        cache[key] = (qs.filter(final_section=section or '', is_third_place=False)
                      .aggregate(r=Max('match_round'))['r'] if qs is not None else None)
    return cache[key]


def podium_stage(match, cache=None):
    """'final' / 'third' / 'semi' / 'quarter' / 'league', or None for an ordinary match."""
    cache = {} if cache is None else cache
    division = match.division
    if division.tournament_type not in ('playoff', 'tree'):
        return None
    if division.tournament_type == 'playoff' and match.phase != 'playoff':
        return None
    if match.is_third_place:
        return 'third'
    if match.bracket_slot is None:
        # Final round-robin of a group + final phase division: every match decides the podium.
        return 'league' if division.final_format == 'league' and division.tournament_type == 'playoff' else None
    last = _last_round(division, match.final_section, cache)
    if last is None:
        return None
    return {0: 'final', 1: 'semi', 2: 'quarter'}.get(last - match.match_round)


def podium_info(match, cache=None):
    """Dict for templates: code, icon, label ("Final"), title ("Final Aleví · Noies"), or None."""
    code = podium_stage(match, cache)
    if code is None:
        return None
    icon, label = _STAGES[code]
    label = label()
    division = match.division
    section = section_label(match.final_section, division.final_split) if match.final_section else ''
    where = division.name + (f' · {section}' if section else '')
    return {'code': code, 'icon': icon, 'label': label, 'where': where,
            'title': f'{label} · {where}', 'major': code in ('final', 'third', 'semi')}


def annotate(matches, cache=None):
    """Set ``.podium`` on every match (None for ordinary ones). Returns the list."""
    cache = {} if cache is None else cache
    for m in matches:
        if m is not None:
            m.podium = podium_info(m, cache)
    return matches
