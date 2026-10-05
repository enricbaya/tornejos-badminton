"""Flexible final phases after a group stage.

A division of type 'playoff' plays groups first and then a final phase:

* knockout (with structural byes when the number is not a power of two) or a
  final round-robin ("lligueta final") between the qualified players;
* qualifiers are either the best N of each group ("Nr.2 gr.3") or the best N
  overall ("Top.4"), ranked across groups by group position, % of matches won,
  set difference and point difference;
* optionally a match for third place (semi-final losers, or the 3rd and 4th
  qualified when there are no semi-finals);
* optionally one separate final phase per gender (or age group) after mixed
  groups — each one stored with ``Match.final_section``.

Placeholder matches keep the source of each side in ``bracket_label`` (the
same "A vs B" format the scheduler always used) and are filled in by
``fill_final_phase`` as soon as the groups they depend on are finished.
"""
import math
import re

from django.utils.translation import gettext as _

TOKEN_RE = re.compile(r'^(Nr\.\d+ gr\.\d+|Top\.\d+)$')
THIRD_PLACE_LABEL = 'Loser SF1 vs Loser SF2'
ABSENT = object()   # a qualifier position that does not exist (e.g. 4th of a group of 3)


# ---------------------------------------------------------------------------
# Sections (separate final phases)
# ---------------------------------------------------------------------------

def team_gender(team):
    genders = {p.gender for p in team.players}
    return genders.pop() if len(genders) == 1 else 'X'


def section_of(team, split):
    if split == 'gender':
        return team_gender(team)
    if split == 'age_group':
        return team.player1.division or ''
    return ''


def section_label(code, split):
    if split == 'gender':
        return {'M': _("Boys"), 'K': _("Girls"), 'X': _("Mixed pairs")}.get(code, code)
    if split == 'age_group':
        return code or _("No age group")
    return ''


def division_sections(division, teams=None):
    """[(code, label, [teams])] in a stable order. One unnamed section when not split."""
    teams = list(teams if teams is not None else division.teams.select_related('player1', 'player2'))
    split = division.final_split
    if not split:
        return [('', '', teams)]
    groups = {}
    for t in teams:
        groups.setdefault(section_of(t, split), []).append(t)
    if split == 'gender':
        order = sorted(groups, key=lambda c: {'M': 0, 'K': 1, 'X': 2}.get(c, 3))
    else:
        from players.models import DivisionCategory
        sort = {c.name: (c.sort_order, c.name) for c in
                DivisionCategory.objects.filter(owner=division.tournament.owner)}
        order = sorted(groups, key=lambda c: sort.get(c, (999, c)))
    return [(c, section_label(c, split), groups[c]) for c in order]


def qualifiers_for(division, code, members):
    """How many players of this section go through to the final phase."""
    if division.final_split:
        try:
            n = int((division.section_qualifiers or {}).get(code, division.final_qualifiers))
        except (TypeError, ValueError):
            n = division.final_qualifiers
    elif division.qualify_mode == 'total':
        n = division.final_qualifiers
    else:
        n = max(1, division.group_count) * max(1, division.advance_count)
    return max(0, min(n, members))


def uses_overall_ranking(division):
    return bool(division.final_split) or division.qualify_mode == 'total'


def seed_tokens(division, count):
    """Qualifier tokens in seed order (best first)."""
    if uses_overall_ranking(division):
        return [f'Top.{i}' for i in range(1, count + 1)]
    g = max(1, division.group_count)
    tokens = []
    pos = 1
    while len(tokens) < count:
        for grp in range(1, g + 1):
            tokens.append(f'Nr.{pos} gr.{grp}')
        pos += 1
    return tokens[:count]


# ---------------------------------------------------------------------------
# Building the final phase
# ---------------------------------------------------------------------------

def _seeding_order(n_slots):
    if n_slots == 1:
        return [1]
    left = _seeding_order(n_slots // 2)
    right = [n_slots + 1 - s for s in left]
    out = []
    for a, b in zip(left, right):
        out.extend([a, b])
    return out


def build_knockout(division, seeds, q, base_round, section='', third_place=False, phase='playoff'):
    """Knockout for the first ``q`` seed tokens. Returns the created matches."""
    from .models import Match
    from .scheduler import _bracket_placeholder_label

    created = []
    if q < 2:
        return created
    total_rounds = math.ceil(math.log2(q))
    size = 2 ** total_rounds
    order = _seeding_order(size)
    slots = [seeds[s - 1] if s <= q else None for s in order]
    common = dict(division=division, team1=None, team2=None, phase=phase, final_section=section)
    for i in range(size // 2):
        a, b = slots[2 * i], slots[2 * i + 1]
        if a is None and b is None:
            continue
        if a is None or b is None:
            created.append(Match.objects.create(
                **common, winner=None, score='Bye', match_round=base_round, bracket_slot=i + 1,
                bracket_label=f'{a or b} (fri)', status='completed', walkover=True))
        else:
            created.append(Match.objects.create(
                **common, match_round=base_round, bracket_slot=i + 1,
                bracket_label=f'{a} vs {b}', status='pending'))
    for r_off in range(1, total_rounds):
        n_slots = size // (2 ** (r_off + 1))
        for s in range(1, n_slots + 1):
            created.append(Match.objects.create(
                **common, match_round=base_round + r_off, bracket_slot=s,
                bracket_label=_bracket_placeholder_label(r_off + 1, total_rounds, n_slots, s),
                status='pending'))
    if third_place:
        final_round = base_round + total_rounds - 1
        if total_rounds >= 2:
            created.append(Match.objects.create(
                **common, match_round=final_round, bracket_slot=2, is_third_place=True,
                bracket_label=THIRD_PLACE_LABEL, status='pending'))
        elif len(seeds) >= 4:
            created.append(Match.objects.create(
                **common, match_round=final_round, bracket_slot=2, is_third_place=True,
                bracket_label=f'{seeds[2]} vs {seeds[3]}', status='pending'))
    return created


def build_league(division, seeds, base_round, section=''):
    """Final round-robin between the qualifier tokens."""
    from .models import Match
    from .scheduler import _round_robin_rounds

    created = []
    for r, pairs in enumerate(_round_robin_rounds(list(seeds)), start=0):
        for a, b in pairs:
            created.append(Match.objects.create(
                division=division, team1=None, team2=None, phase='playoff', final_section=section,
                match_round=base_round + r, bracket_slot=None, bracket_label=f'{a} vs {b}', status='pending'))
    return created


def build_final_phase(division, base_round, groups):
    """Create the whole final phase of a 'playoff' division (after its group stage)."""
    created = []
    members_by_section = division_sections(division, [t for g in groups for t in g])
    for code, _label, members in members_by_section:
        q = qualifiers_for(division, code, len(members))
        if q < 2:
            continue
        extra = 2 if (division.third_place_match and q == 2 and len(members) >= 4) else 0
        seeds = seed_tokens(division, q + extra)
        if division.final_format == 'league':
            created += build_league(division, seeds[:q], base_round, section=code)
        else:
            created += build_knockout(division, seeds, q, base_round, section=code,
                                      third_place=division.third_place_match)
    return created


# ---------------------------------------------------------------------------
# Filling the placeholders once the groups are played
# ---------------------------------------------------------------------------

def _overall_ranking(division, teams):
    """Teams ranked across groups: group position, % won, set difference, point difference.

    The group position only counts the teams being ranked (e.g. the 2nd girl of a
    mixed group is "2nd" for the girls' final, whatever the boys did).
    """
    from .standings import _parse_score, _parse_sets, compute_group_standings
    members = {t.pk for t in teams}
    pos = {}
    for _g, rows in compute_group_standings(division):
        own = [r for r in rows if r['team'].pk in members]
        for i, r in enumerate(own, start=1):
            pos[r['team'].pk] = i
    tot = {}
    for m in division.matches.filter(phase='group', status='completed').exclude(team2__isnull=True):
        p1, p2 = _parse_score(m.score)
        s1, s2 = _parse_sets(m.score)
        for pk, sw, sl, pf, pa in ((m.team1_id, s1, s2, p1, p2), (m.team2_id, s2, s1, p2, p1)):
            t = tot.setdefault(pk, [0, 0, 0, 0])
            t[0] += 1
            t[1] += 1 if m.winner_id == pk else 0
            t[2] += sw - sl
            t[3] += pf - pa

    def key(team):
        played, won, sd, pd = tot.get(team.pk, [0, 0, 0, 0])
        return (pos.get(team.pk, 999), -(won / played if played else 0), -sd, -pd, team.name.lower())
    return sorted(teams, key=key)


def _token_map(division):
    """{(section, token): team | ABSENT} for every qualifier that is already known."""
    from .standings import compute_group_standings
    out = {}
    group_matches = division.matches.filter(phase='group')
    unfinished = set(group_matches.exclude(status='completed').values_list('group_number', flat=True))
    for gnum, rows in compute_group_standings(division):
        if gnum in unfinished:
            continue
        for pos in range(1, 17):
            out[('', f'Nr.{pos} gr.{gnum}')] = rows[pos - 1]['team'] if pos <= len(rows) else ABSENT
    if not unfinished and group_matches.exists():
        for code, _label, members in division_sections(division):
            ranked = _overall_ranking(division, members)
            for i in range(1, 65):
                out[(code, f'Top.{i}')] = ranked[i - 1] if i <= len(ranked) else ABSENT
    return out


def _resolve(tmap, section, token):
    return tmap.get((section, token), tmap.get(('', token)))


def fill_final_phase(division):
    """Put the qualified players into the final-phase placeholders that can be resolved now."""
    from .scheduler import _advance_bracket_inline

    tmap = _token_map(division)
    if not tmap:
        return
    for m in division.matches.filter(phase='playoff').exclude(bracket_label__isnull=True).exclude(bracket_label=''):
        label = m.bracket_label
        if label.endswith(' (fri)'):
            team = _resolve(tmap, m.final_section, label[:-6].strip())
            if team not in (None, ABSENT) and m.team1_id is None:
                m.team1 = team
                m.winner = team
                m.save(update_fields=['team1', 'winner'])
                _advance_bracket_inline(m)
            continue
        if m.status != 'pending' or ' vs ' not in label:
            continue
        a, b = (x.strip() for x in label.split(' vs ', 1))
        if not (TOKEN_RE.match(a) and TOKEN_RE.match(b)):
            continue
        ta, tb = _resolve(tmap, m.final_section, a), _resolve(tmap, m.final_section, b)
        fields = []
        if m.team1_id is None and ta not in (None, ABSENT):
            m.team1 = ta
            fields.append('team1')
        if m.team2_id is None and tb not in (None, ABSENT):
            m.team2 = tb
            fields.append('team2')
        one_missing = (ta is ABSENT) != (tb is ABSENT)
        if one_missing and m.bracket_slot is not None:
            # A qualifier position that does not exist (smaller group): the other one goes through.
            present = m.team1 or m.team2
            if present is not None:
                m.team1, m.team2, m.winner = present, None, present
                m.score, m.status, m.walkover = 'Bye', 'completed', True
                m.save(update_fields=['team1', 'team2', 'winner', 'score', 'status', 'walkover'])
                _advance_bracket_inline(m)
                continue
        if m.team1_id and m.team2_id:
            m.bracket_label = None
            fields.append('bracket_label')
        if fields:
            m.save(update_fields=fields)


# ---------------------------------------------------------------------------
# Reading the final phase (brackets, third place, final round-robin)
# ---------------------------------------------------------------------------

def league_rows(matches):
    """Standings of a final round-robin from its matches."""
    from .standings import _parse_score, _parse_sets
    rows = {}
    for m in matches:
        for t in (m.team1, m.team2):
            if t is not None and t.pk not in rows:
                rows[t.pk] = {'team': t, 'played': 0, 'won': 0, 'lost': 0, 'points': 0, 'sd': 0, 'pd': 0}
    for m in matches:
        if m.status != 'completed' or not (m.team1_id and m.team2_id):
            continue
        p1, p2 = _parse_score(m.score)
        s1, s2 = _parse_sets(m.score)
        for pk, sw, sl, pf, pa in ((m.team1_id, s1, s2, p1, p2), (m.team2_id, s2, s1, p2, p1)):
            r = rows[pk]
            r['played'] += 1
            r['sd'] += sw - sl
            r['pd'] += pf - pa
        if m.winner_id in rows:
            rows[m.winner_id]['won'] += 1
            rows[m.winner_id]['points'] += 2
            loser = m.team2_id if m.winner_id == m.team1_id else m.team1_id
            if loser in rows:
                rows[loser]['lost'] += 1
    return sorted(rows.values(), key=lambda r: (-r['points'], -r['sd'], -r['pd'], r['team'].name.lower()))


def section_podiums(division):
    """[{code, label, gold: [teams], silver, bronze, done}] for each final phase of the division."""
    qs = division.matches.select_related('team1__player1', 'team1__player2', 'team2__player1', 'team2__player2', 'winner')
    if division.tournament_type == 'playoff':
        qs = qs.filter(phase='playoff')
    matches = list(qs)
    codes = []
    for m in matches:
        if m.final_section not in codes:
            codes.append(m.final_section)
    order = {c: i for i, (c, _l, _t) in enumerate(division_sections(division))}
    codes.sort(key=lambda c: order.get(c, 99))
    out = []
    for code in codes:
        ms = [m for m in matches if m.final_section == code]
        gold, silver, bronze = [], [], []
        league = [m for m in ms if m.bracket_slot is None]
        if league:
            rows = league_rows(league)
            if all(m.status == 'completed' for m in league):
                gold = [r['team'] for r in rows[:1]]
                silver = [r['team'] for r in rows[1:2]]
                bronze = [r['team'] for r in rows[2:3]]
        else:
            ko = [m for m in ms if not m.is_third_place and m.bracket_slot is not None]
            if ko:
                final_round = max(m.match_round for m in ko)
                final = next((m for m in ko if m.match_round == final_round), None)
                if final and final.status == 'completed' and final.winner_id:
                    gold = [final.winner]
                    loser = final.team2 if final.winner_id == final.team1_id else final.team1
                    silver = [loser] if loser else []
                third = next((m for m in ms if m.is_third_place), None)
                if third is not None:
                    if third.status == 'completed' and third.winner_id:
                        bronze = [third.winner]
                elif division.bronze_count:
                    for semi in [m for m in ko if m.match_round == final_round - 1][:division.bronze_count]:
                        if semi.winner_id and semi.team2_id:
                            lo = semi.team2 if semi.winner_id == semi.team1_id else semi.team1
                            if lo:
                                bronze.append(lo)
        out.append({'code': code, 'label': section_label(code, division.final_split),
                    'gold': gold[:max(1, division.gold_count)] if gold else [],
                    'silver': silver, 'bronze': bronze})
    return out
