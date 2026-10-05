"""Medals per age group and gender inside one tournament category.

A tournament category can mix several age groups and both genders (e.g. Benjamí and
Aleví, boys and girls) while the club still gives three medals to every real
combination. All players of the category are ranked together, whatever group they
played in, with the criteria agreed with the club, in this order:

1. stage reached (champion, finalist, semi-finalist, quarter-finalist…)
2. position in their group
3. % of matches won
4. set difference
5. point difference

The best three of each age group × gender get gold, silver and bronze.
"""
from django.utils.translation import gettext as _

from .standings import _parse_score, _parse_sets, compute_group_standings, compute_standings

PODIUM = ('gold', 'silver', 'bronze')


def _gender_of(team):
    genders = {p.gender for p in team.players}
    if len(genders) == 1:
        return genders.pop()
    return 'X'  # mixed pair


def _gender_label(code):
    return {'M': _("boys"), 'K': _("girls"), 'X': _("mixed")}.get(code, '')


def subgroup_key(team):
    """(age group, gender code) of a team. Pairs use the first player's age group."""
    p = team.player1
    return (p.division or '', _gender_of(team))


def subgroup_code(key):
    """Language-independent id stored with manual corrections: 'Benjamí|K'."""
    return f'{key[0]}|{key[1]}'[:80]


def subgroup_labels(teams):
    """Readable label per key. The gender is only added when the age group has both genders."""
    by_group = {}
    for t in teams:
        tag, g = subgroup_key(t)
        by_group.setdefault(tag, set()).add(g)
    labels = {}
    for tag, genders in by_group.items():
        for g in genders:
            name = tag or _("No age group")
            labels[(tag, g)] = f"{name} · {_gender_label(g)}" if len(genders) > 1 else name
    return labels


def _stage_reached(division):
    """team pk → how far the team got in the final phase (higher is better; 0 = group stage only).

    Knockout: the round reached, +1 for the champion, +0.5 for the winner of the
    third-place match. Final round-robin: 1 plus a fraction for the final position.
    Each separate final phase (per gender / age group) is measured on its own.
    """
    if division.tournament_type not in ('playoff', 'tree'):
        return {}
    from .final_phase import league_rows
    qs = division.matches.select_related('team1', 'team2')
    if division.tournament_type == 'playoff':
        qs = qs.filter(phase='playoff')
    matches = list(qs)
    stage = {}
    for code in {m.final_section for m in matches}:
        ms = [m for m in matches if m.final_section == code]
        league = [m for m in ms if m.bracket_slot is None]
        if league:
            rows = league_rows(league)
            n = len(rows)
            for i, r in enumerate(rows):
                stage[r['team'].pk] = 1 + (n - i) / (n + 1)
            continue
        ko = [m for m in ms if not m.is_third_place]
        if not ko:
            continue
        first_round = min(m.match_round for m in ko)
        final_round = max(m.match_round for m in ko)
        for m in ko:
            for pk in (m.team1_id, m.team2_id):
                if pk:
                    stage[pk] = max(stage.get(pk, 0), m.match_round - first_round + 1)
        for m in ko:
            if m.match_round == final_round and m.status == 'completed' and m.winner_id:
                stage[m.winner_id] = final_round - first_round + 2  # champion
        for m in ms:
            if m.is_third_place and m.status == 'completed' and m.winner_id:
                stage[m.winner_id] = stage.get(m.winner_id, 0) + 0.5
    return stage


def _group_position(division):
    """team pk → position in their group (or in the single round-robin)."""
    pos = {}
    if division.tournament_type == 'group':
        for i, r in enumerate(compute_standings(division), start=1):
            pos[r['team'].pk] = i
    elif division.has_groups:
        for _g, rows in compute_group_standings(division):
            for i, r in enumerate(rows, start=1):
                pos[r['team'].pk] = i
    return pos


def _match_totals(division):
    """team pk → dict(played, won, set_diff, point_diff) over every completed match of the category."""
    tot = {}
    for m in division.matches.filter(status='completed').exclude(team2__isnull=True):
        if not m.team1_id or not m.team2_id:
            continue
        p1, p2 = _parse_score(m.score)
        s1, s2 = _parse_sets(m.score)
        for pk, sw, sl, pf, pa in ((m.team1_id, s1, s2, p1, p2), (m.team2_id, s2, s1, p2, p1)):
            t = tot.setdefault(pk, {'played': 0, 'won': 0, 'set_diff': 0, 'point_diff': 0})
            t['played'] += 1
            t['won'] += 1 if m.winner_id == pk else 0
            t['set_diff'] += sw - sl
            t['point_diff'] += pf - pa
    return tot


def rank_teams(division):
    """All teams of the category ranked with the agreed criteria. Each item: dict(team, stage, group_pos, …)."""
    teams = list(division.teams.select_related('player1', 'player2'))
    stage = _stage_reached(division)
    gpos = _group_position(division)
    tot = _match_totals(division)
    rows = []
    for t in teams:
        x = tot.get(t.pk, {'played': 0, 'won': 0, 'set_diff': 0, 'point_diff': 0})
        pct = x['won'] / x['played'] if x['played'] else 0
        rows.append({
            'team': t, 'stage': stage.get(t.pk, 0), 'group_pos': gpos.get(t.pk),
            'played': x['played'], 'won': x['won'], 'win_pct': round(pct * 100),
            'set_diff': x['set_diff'], 'point_diff': x['point_diff'],
            '_key': (-stage.get(t.pk, 0), gpos.get(t.pk, 999), -pct, -x['set_diff'], -x['point_diff'], t.name.lower()),
        })
    rows.sort(key=lambda r: r['_key'])
    return rows


def compute_subgroup_medals(division):
    """List of dicts (key, label, ranking, gold, silver, bronze), age groups in alphabetical order.

    Manual corrections (MedalOverride with this subgroup label) replace the computed podium of that subgroup.
    """
    ranking = rank_teams(division)
    labels = subgroup_labels([r['team'] for r in ranking])
    overrides = {}
    for o in division.medal_overrides.exclude(subgroup='').select_related('team').order_by('order'):
        overrides.setdefault(o.subgroup, {}).setdefault(o.medal, []).append(o.team)
    groups = {}
    for r in ranking:
        groups.setdefault(subgroup_key(r['team']), []).append(r)
    result = []
    for key in sorted(groups, key=lambda k: (k[0].lower(), k[1])):
        rows = groups[key]
        label = labels[key]
        code = subgroup_code(key)
        entry = {'key': key, 'code': code, 'label': label, 'ranking': rows, 'override': code in overrides}
        if code in overrides:
            for medal in PODIUM:
                entry[medal] = overrides[code].get(medal, [])
        else:
            for i, medal in enumerate(PODIUM):
                entry[medal] = [rows[i]['team']] if i < len(rows) else []
        result.append(entry)
    return result
