"""Suggest a competition format from the number of participants in a division.

The goal for school tournaments is that every child plays several matches while
the whole day still fits on the available courts:

* up to 6 participants  → one round-robin (everyone plays everyone)
* 7 or more             → groups of 4–5 + knockout with the best 2 of each group
                          (only the group winners when there are more than 4 groups)

The alternative "several groups without playoff" is always offered for divisions
with 7+ participants, for days where finals are not wanted.
"""
import math

from django.utils.translation import gettext as _


def _group_sizes(n, group_count):
    """Sizes of the groups when n teams are spread snake-wise over group_count groups."""
    base, extra = divmod(n, group_count)
    return [base + (1 if i < extra else 0) for i in range(group_count)]


def _round_robin_matches(sizes):
    return sum(k * (k - 1) // 2 for k in sizes)


def _bracket_matches(advancing):
    """Real matches in a knockout of `advancing` teams (byes are not matches)."""
    return max(0, advancing - 1)


def _option(tournament_type, n, group_count=1, advance_count=0):
    if tournament_type == 'group':
        sizes = [n]
    elif tournament_type == 'tree':
        sizes = []
    else:
        sizes = _group_sizes(n, group_count)
    matches = _round_robin_matches(sizes)
    if tournament_type == 'playoff':
        matches += _bracket_matches(group_count * advance_count)
    elif tournament_type == 'tree':
        matches = _bracket_matches(n)

    if tournament_type == 'group':
        label = _("One round-robin: everyone plays everyone")
    elif tournament_type == 'tree':
        label = _("Single elimination")
    elif tournament_type == 'groups':
        label = _("%(g)s round-robin groups of %(sizes)s, no playoff") % {
            'g': group_count, 'sizes': '/'.join(str(s) for s in sorted(set(sizes), reverse=True))}
    else:
        stage = {2: _("final"), 4: _("semi-finals"), 8: _("quarter-finals"), 16: _("round of 16")}.get(
            2 ** math.ceil(math.log2(max(2, group_count * advance_count))), _("playoff"))
        label = _("%(g)s groups of %(sizes)s + %(stage)s (top %(a)s of each group)") % {
            'g': group_count, 'sizes': '/'.join(str(s) for s in sorted(set(sizes), reverse=True)),
            'stage': stage, 'a': advance_count}

    min_per_team = min(sizes) - 1 if sizes else 1
    return {
        'tournament_type': tournament_type,
        'group_count': group_count,
        'advance_count': advance_count,
        'label': label,
        'matches': matches,
        'min_matches_per_team': min_per_team,
    }


def suggest_formats(n):
    """Return a list of format options for ``n`` participants, best suggestion first.

    Groups aim for 4–5 participants so every child plays at least 3 group matches.
    Each option is a dict with tournament_type, group_count, advance_count,
    label, matches (total) and min_matches_per_team.
    """
    if n < 2:
        return []
    if n <= 6:
        return [_option('group', n)]
    groups = max(2, round(n / 4.5))
    # Keep the knockout at 8 players or fewer: top 2 per group up to 4 groups, else only the winner.
    advance = 2 if groups <= 4 else 1
    options = [
        _option('playoff', n, group_count=groups, advance_count=advance),
        _option('groups', n, group_count=groups),
    ]
    if n <= 8:
        options.append(_option('group', n))
    return options


def estimated_court_minutes(option, match_minutes):
    """Total court time the option needs, in minutes."""
    return option['matches'] * match_minutes
