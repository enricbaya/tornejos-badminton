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


# ---------------------------------------------------------------------------
# Free configuration: evaluate any format and suggest the ones that give
# every player about 3–4 matches and never more than 5.
# ---------------------------------------------------------------------------

TARGET_MIN = 3
TARGET_MAX = 5


def _stage_name(q):
    size = 2 ** math.ceil(math.log2(max(2, q)))
    return {2: _("final"), 4: _("semi-finals"), 8: _("quarter-finals"), 16: _("round of 16")}.get(size, _("knockout"))


def normalise_config(cfg, n):
    """Fill defaults and derive tournament_type from a free configuration dict."""
    c = {
        'group_count': max(1, min(16, int(cfg.get('group_count') or 1))),
        'final_format': cfg.get('final_format') or 'none',        # none | knockout | league
        'qualify_mode': cfg.get('qualify_mode') or 'per_group',   # per_group | total
        'advance_count': max(1, min(8, int(cfg.get('advance_count') or 1))),
        'final_qualifiers': max(2, min(32, int(cfg.get('final_qualifiers') or 4))),
        'third_place_match': bool(cfg.get('third_place_match')),
        'final_split': cfg.get('final_split') or '',
        'section_qualifiers': dict(cfg.get('section_qualifiers') or {}),
    }
    c['group_count'] = min(c['group_count'], max(1, n // 2))
    if c['final_format'] == 'none':
        c['tournament_type'] = 'group' if c['group_count'] == 1 else 'groups'
        c['third_place_match'] = False
        c['final_split'] = ''
    else:
        c['tournament_type'] = 'playoff'
    if c['final_split']:
        c['qualify_mode'] = 'total'
    if c['final_format'] == 'league':
        c['third_place_match'] = False
    return c


def evaluate_format(cfg, n, section_counts=None, match_minutes=None, target=None):
    """Matches in total and per player (min/max) for a configuration with n participants.

    section_counts: {code: players} for a final phase split per gender / age group.
    """
    c = normalise_config(cfg, n)
    sizes = _group_sizes(n, c['group_count'])
    total = _round_robin_matches(sizes)
    base_min, base_max = min(sizes) - 1, max(sizes) - 1
    extra_min = extra_max = 0
    qualified = 0
    if c['final_format'] != 'none':
        if c['final_split']:
            sections = [(code, cnt, int(c['section_qualifiers'].get(code, c['final_qualifiers']) or 0))
                        for code, cnt in (section_counts or {'': n}).items()]
        elif c['qualify_mode'] == 'total':
            sections = [('', n, c['final_qualifiers'])]
        else:
            sections = [('', n, c['group_count'] * c['advance_count'])]
        mins, maxs = [], []
        for _code, members, q in sections:
            q = max(0, min(q, members))
            if q < 2:
                continue
            if c['final_format'] == 'league':
                total += q * (q - 1) // 2
                mins.append(q - 1)
                maxs.append(q - 1)
                qualified += q
            else:
                rounds = math.ceil(math.log2(q))
                total += q - 1
                qualified += q
                mins.append(1)
                maxs.append(rounds)
                if c['third_place_match']:
                    if rounds >= 2:
                        total += 1
                    elif members >= 4:
                        total += 1   # 3rd and 4th qualified play for bronze
                        qualified += 2
        if mins:
            extra_min, extra_max = min(mins), max(maxs)
    min_pp = base_min if qualified < n else base_min + extra_min
    max_pp = base_max + extra_max
    res = {
        'config': c,
        'tournament_type': c['tournament_type'],
        'group_count': c['group_count'],
        'advance_count': c['advance_count'],
        'final_format': c['final_format'],
        'qualify_mode': c['qualify_mode'],
        'final_qualifiers': c['final_qualifiers'],
        'third_place_match': c['third_place_match'],
        'final_split': c['final_split'],
        'matches': total,
        'min_matches_per_team': min_pp,
        'max_matches_per_team': max_pp,
        'sizes': sizes,
        'label': describe_format(c, sizes),
        'ok': (target or (TARGET_MIN, TARGET_MAX))[0] <= min_pp and max_pp <= (target or (TARGET_MIN, TARGET_MAX))[1],
    }
    if match_minutes:
        res['court_minutes'] = total * match_minutes
    return res


def describe_format(c, sizes):
    g = c['group_count']
    size_txt = '/'.join(str(s) for s in sorted(set(sizes), reverse=True))
    if g == 1:
        base = _("One round-robin of %(n)s") % {'n': sizes[0]}
    else:
        base = _("%(g)s groups of %(sizes)s") % {'g': g, 'sizes': size_txt}
    ff = c['final_format']
    if ff == 'none':
        return base + (_(", no final phase") if g > 1 else '')
    if c['final_split']:
        who = _("separate final phase per gender") if c['final_split'] == 'gender' else _("separate final phase per age group")
        kind = _("final round-robin") if ff == 'league' else _("knockout")
        txt = f"{base} + {kind}, {who}"
    elif ff == 'league':
        q = c['final_qualifiers'] if c['qualify_mode'] == 'total' else g * c['advance_count']
        txt = base + ' + ' + _("final round-robin of the best %(q)s") % {'q': q}
    elif c['qualify_mode'] == 'total':
        q = c['final_qualifiers']
        txt = base + ' + ' + _("%(stage)s between the best %(q)s") % {'stage': _stage_name(q), 'q': q}
    else:
        q = g * c['advance_count']
        if c['advance_count'] >= max(sizes):
            txt = base + ' + ' + _("%(stage)s with everybody, crossing the groups (1st v last…)") % {'stage': _stage_name(q)}
        elif c['advance_count'] == 1:
            txt = base + ' + ' + _("%(stage)s (the winner of each group)") % {'stage': _stage_name(q)}
        else:
            txt = base + ' + ' + _("%(stage)s (top %(a)s of each group)") % {'stage': _stage_name(q), 'a': c['advance_count']}
    if c['third_place_match']:
        txt += ' + ' + _("match for 3rd place")
    return txt


def _score(opt, n, target=None):
    """Lower is better: inside the wished range of matches per player, close to its middle, short day."""
    lo, hi = target or (TARGET_MIN, TARGET_MAX)
    avg = opt['matches'] * 2 / n
    penalty = 100 * max(0, lo - opt['min_matches_per_team'])
    penalty += 100 * max(0, opt['max_matches_per_team'] - hi)
    penalty += abs(avg - (lo + hi) / 2 + 0.25) * 8
    penalty += opt['matches'] / 10
    if opt['final_format'] != 'none':
        penalty -= 3   # a final phase makes the medals clearer
    return penalty


def suggest_flexible(n, section_counts=None, match_minutes=None, limit=4, target=None):
    """Several good formats for n participants, best first (all fields needed to apply them)."""
    if n < 2:
        return []
    candidates = []
    seen = set()
    max_groups = max(1, n // 3)
    for g in range(1, max_groups + 1):
        sizes = _group_sizes(n, g)
        if g > 1 and min(sizes) < 3:
            continue
        configs = [{'group_count': g, 'final_format': 'none'}]
        if g > 1:
            for a in (1, 2):
                if 2 <= g * a <= 8:
                    configs.append({'group_count': g, 'final_format': 'knockout', 'advance_count': a})
                    if g * a >= 4:
                        configs.append({'group_count': g, 'final_format': 'knockout', 'advance_count': a,
                                        'third_place_match': True})
            for q in (2, 4):
                if q < n and q % g != 0:
                    configs.append({'group_count': g, 'final_format': 'knockout', 'qualify_mode': 'total',
                                    'final_qualifiers': q})
            if 4 <= n:
                configs.append({'group_count': g, 'final_format': 'league', 'qualify_mode': 'total', 'final_qualifiers': 3})
                configs.append({'group_count': g, 'final_format': 'league', 'qualify_mode': 'total', 'final_qualifiers': 4})
        for cfg in configs:
            opt = evaluate_format(cfg, n, section_counts, match_minutes, target)
            key = (opt['label'], opt['matches'])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(opt)
    candidates.sort(key=lambda o: _score(o, n, target))
    return candidates[:limit]
