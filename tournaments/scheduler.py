"""
Scheduling logic for generating match schedules based on tournament type.
- Round-robin: all teams play against each other (group tournaments)
- Bracket: single elimination bracket (tree tournaments)
- Playoff: group stage (round-robin per group) + single-elimination bracket
"""
import itertools
import math

from django.utils.translation import gettext_lazy as _


def _sort_teams_by_seed(division, teams):
    """
    Sort teams for bracket/playoff seeding:
    - Seeded teams (with a DivisionSeed entry) come first, in ascending seed number order.
    - Unseeded teams follow, sorted alphabetically by team name.
    """
    from .models import DivisionSeed
    seeds = {s.team_id: s.seed_number for s in DivisionSeed.objects.filter(division=division)}
    seeded = sorted([t for t in teams if t.pk in seeds], key=lambda t: seeds[t.pk])
    unseeded = sorted([t for t in teams if t.pk not in seeds], key=lambda t: t.name)
    return seeded + unseeded


# ---------------------------------------------------------------------------
# Round-robin
# ---------------------------------------------------------------------------

def _round_robin_rounds(teams):
    """
    Circle-method round-robin: returns a list of rounds, each round being a
    list of (team_a, team_b) pairs.  teams is assumed to already be sorted.
    If len(teams) is odd a dummy None is appended; pairs involving None are
    bye-matches and are excluded from each round's output.
    """
    if len(teams) % 2 == 1:
        teams = teams + [None]  # bye placeholder
    n = len(teams)
    rounds = []
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            t1 = teams[i]
            t2 = teams[n - 1 - i]
            if t1 is not None and t2 is not None:
                pairs.append((t1, t2))
        rounds.append(pairs)
        # Rotate: keep teams[0] fixed, rotate the rest
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return rounds


def generate_round_robin(division):
    """
    Generate a full round-robin schedule using the circle method.
    Each round contains floor(n/2) simultaneous matches so that the
    time-slot scheduler can run them on parallel courts.
    """
    from .models import Match

    teams = list(division.teams.all())
    if len(teams) < 2:
        return []

    Match.objects.filter(division=division, status='pending').delete()

    teams = sorted(teams, key=lambda t: t.player1.name)
    rounds = _round_robin_rounds(teams)
    created = []
    for round_num, pairs in enumerate(rounds, start=1):
        for team1, team2 in pairs:
            match = Match.objects.create(
                division=division,
                team1=team1,
                team2=team2,
                match_round=round_num,
                status='pending',
            )
            created.append(match)
    return created


# ---------------------------------------------------------------------------
# Single-elimination bracket
# ---------------------------------------------------------------------------

def _seeding_order(n_slots):
    """
    Return the seed numbers placed in each bracket slot (standard seeding).
    Example: n_slots=4 → [1, 4, 2, 3] → pairs (1 vs 4) and (2 vs 3).
    """
    if n_slots == 1:
        return [1]
    half = n_slots // 2
    left = _seeding_order(half)
    right = [n_slots + 1 - s for s in left]
    result = []
    for l, r in zip(left, right):
        result.extend([l, r])
    return result


def get_round_label(round_num, total_rounds):
    """Return a translated label for the given bracket round."""
    from_end = total_rounds - round_num + 1
    labels = {
        1: _('Final'),
        2: _('Semifinal'),
        3: _('Quarterfinal'),
        4: _('Round of 16'),
    }
    if from_end in labels:
        return labels[from_end]
    return _('Round %(n)s') % {'n': round_num}


def _bracket_placeholder_label(round_num, total_rounds, n_slots, slot_num):
    """Human-readable label for a not-yet-determined bracket match.

    Stored in DB; rendered as-is in templates. Forced to English so the
    stored label matches the source language used in the .po file (existing
    Danish labels in legacy data continue to render unchanged).
    """
    from django.utils.translation import override
    with override('en'):
        name = str(get_round_label(round_num, total_rounds))
    return name if n_slots == 1 else f'{name} {slot_num}'


def generate_bracket(division):
    """
    Pre-create the ENTIRE single-elimination bracket structure:
      - Round 1: real matches (with teams) or bye (team2=None, auto-completed).
      - Round 2+: placeholder matches (team1=None, team2=None, bracket_label set).

    Byes are auto-advanced so the next-round placeholder immediately gets team1 filled.
    Returns all created Match objects ordered by (match_round, bracket_slot).
    """
    from .models import Match

    teams = list(division.teams.all())
    n = len(teams)
    if n < 2:
        return []

    teams = _sort_teams_by_seed(division, teams)

    total_rounds = math.ceil(math.log2(n))
    bracket_size = 2 ** total_rounds

    # Delete ALL existing matches for a clean regeneration
    Match.objects.filter(division=division).delete()

    seed_order = _seeding_order(bracket_size)
    slots = [teams[s - 1] if s <= n else None for s in seed_order]

    created = []

    # ── Round 1 ──────────────────────────────────────────────────────────
    for i in range(0, bracket_size, 2):
        slot_num = i // 2 + 1
        t1, t2 = slots[i], slots[i + 1]

        if t1 is None and t2 is None:
            continue
        if t1 is None:
            t1, t2 = t2, None  # normalise: real team in t1

        if t2 is None:
            match = Match.objects.create(
                division=division,
                team1=t1, team2=None,
                winner=t1, score='Bye',
                match_round=1, bracket_slot=slot_num,
                status='completed', walkover=True,
            )
        else:
            match = Match.objects.create(
                division=division,
                team1=t1, team2=t2,
                match_round=1, bracket_slot=slot_num,
                status='pending',
            )
        created.append(match)

    # ── Round 2+ placeholders ────────────────────────────────────────────
    for r in range(2, total_rounds + 1):
        n_slots = bracket_size // (2 ** r)
        for s in range(1, n_slots + 1):
            match = Match.objects.create(
                division=division,
                team1=None, team2=None,
                match_round=r, bracket_slot=s,
                bracket_label=_bracket_placeholder_label(r, total_rounds, n_slots, s),
                status='pending',
            )
            created.append(match)

    # ── Match for third place ────────────────────────────────────────────
    if division.third_place_match and total_rounds >= 2:
        from .final_phase import THIRD_PLACE_LABEL
        created.append(Match.objects.create(
            division=division, team1=None, team2=None, match_round=total_rounds, bracket_slot=2,
            is_third_place=True, bracket_label=THIRD_PLACE_LABEL, status='pending'))

    # ── Auto-advance byes ────────────────────────────────────────────────
    for match in [m for m in created if m.team2 is None and m.status == 'completed']:
        _advance_bracket_inline(match)

    return created


def _advance_bracket_inline(match):
    """
    Fill in a next-round placeholder after a bracket match completes.
    Used internally (during generation for byes, and after result recording).
    Works for both 'tree' and 'playoff' (bracket-phase) divisions.
    """
    from .models import Match

    division = match.division
    if division.tournament_type not in ('tree', 'playoff'):
        return
    # Group-phase matches in a playoff division are handled by
    # fill_playoff_bracket_from_group instead.
    if division.tournament_type == 'playoff' and match.phase == 'group':
        return
    if not match.winner or match.bracket_slot is None or match.is_third_place:
        return

    next_round = match.match_round + 1
    next_slot = math.ceil(match.bracket_slot / 2)
    is_odd = (match.bracket_slot % 2 == 1)

    same_bracket = Match.objects.filter(division=division, final_section=match.final_section,
                                        bracket_slot__isnull=False, is_third_place=False)
    if division.tournament_type == 'playoff':
        same_bracket = same_bracket.filter(phase='playoff')
    _send_loser_to_third_place(match, same_bracket)

    qs = same_bracket.filter(match_round=next_round, bracket_slot=next_slot)

    placeholder = qs.first()

    if not placeholder:
        return  # No further rounds (match was the final)

    if is_odd:
        placeholder.team1 = match.winner
    else:
        placeholder.team2 = match.winner

    # If both teams are now known: clear placeholder label
    if placeholder.team1 and placeholder.team2:
        placeholder.bracket_label = None

    placeholder.save(update_fields=['team1', 'team2', 'bracket_label'])


def _send_loser_to_third_place(match, same_bracket):
    """After a semi-final: the loser goes to the third-place match of the same bracket."""
    from .models import Match
    if not match.team1_id or not match.team2_id:
        return
    final_round = max(same_bracket.values_list('match_round', flat=True), default=None)
    if final_round is None or match.match_round != final_round - 1:
        return
    third = Match.objects.filter(division=match.division, final_section=match.final_section,
                                 is_third_place=True, status='pending').first()
    if third is None:
        return
    loser = match.team2 if match.winner_id == match.team1_id else match.team1
    if match.bracket_slot % 2 == 1:
        third.team1 = loser
    else:
        third.team2 = loser
    if third.team1_id and third.team2_id:
        third.bracket_label = None
    third.save(update_fields=['team1', 'team2', 'bracket_label'])


def advance_bracket(match):
    """Public entry point – called after a match result is saved."""
    _advance_bracket_inline(match)


def fill_playoff_bracket_from_group(division, group_number):
    """
    Called after any group-stage match in a playoff division completes: puts the
    qualified players into the final-phase placeholders that can be resolved now
    (a finished group fills "Nr.X gr.Y"; all groups finished fill "Top.N").
    """
    from .final_phase import fill_final_phase
    if group_number is None:
        return
    fill_final_phase(division)


def _bracket_rounds(matches):
    """Visual rounds of one knockout bracket (list of matches with bracket_slot)."""
    if not matches:
        return [], 0
    min_round = min(m.match_round for m in matches)
    round1 = [m for m in matches if m.match_round == min_round]
    bracket_size = 2 ** math.ceil(math.log2(max(2, max(m.bracket_slot for m in round1) * 2)))
    total_rounds = int(math.log2(bracket_size)) if bracket_size > 1 else 1
    lookup = {(m.match_round, m.bracket_slot): m for m in matches}
    BASE_HEIGHT = 80  # px per round-1 slot
    rounds = []
    for r_offset in range(total_rounds):
        r = min_round + r_offset
        n_slots = bracket_size // (2 ** (r_offset + 1))
        rounds.append({
            'label': get_round_label(r_offset + 1, total_rounds),
            'round': r,
            'is_last': r_offset == total_rounds - 1,
            'slot_height': BASE_HEIGHT * (2 ** r_offset),
            'slots': [{'slot': s, 'match': lookup.get((r, s)), 'is_odd': s % 2 == 1}
                      for s in range(1, n_slots + 1)],
        })
    return rounds, total_rounds


def get_bracket_data(division):
    """
    Build the visualisation data of the final phase of a tree or playoff division.

    Returns None when there is nothing to show, else a dict with ``sections``:
    one per final phase (one, or one per gender / age group), each with the
    knockout ``rounds``, the ``third_place`` match and/or the final round-robin
    (``league_rows`` + ``league_matches``). ``rounds``/``total_rounds`` of the
    first section are kept at the top level for older templates.
    """
    from .final_phase import division_sections, league_rows, section_label

    qs = division.matches.select_related('team1', 'team2', 'winner').order_by('match_round', 'bracket_slot', 'pk')
    if division.tournament_type == 'playoff':
        qs = qs.filter(phase='playoff')
    matches = list(qs)
    if not matches:
        return None
    order = {c: i for i, (c, _l, _t) in enumerate(division_sections(division))}
    codes = sorted({m.final_section for m in matches}, key=lambda c: order.get(c, 99))
    sections = []
    for code in codes:
        ms = [m for m in matches if m.final_section == code]
        ko = [m for m in ms if m.bracket_slot is not None and not m.is_third_place]
        league = [m for m in ms if m.bracket_slot is None]
        rounds, total = _bracket_rounds(ko)
        sections.append({
            'code': code,
            'label': section_label(code, division.final_split),
            'rounds': rounds,
            'total_rounds': total,
            'third_place': next((m for m in ms if m.is_third_place), None),
            'league_matches': league,
            'league_rows': league_rows(league) if league else [],
        })
    first = sections[0]
    return {'rounds': first['rounds'], 'total_rounds': first['total_rounds'], 'sections': sections}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
# Playoff (group stage + bracket)
# ---------------------------------------------------------------------------

def generate_playoff(division, with_bracket=True):
    """
    Generate a playoff schedule:
    1. Divide teams into groups and generate round-robin within each group.
    2. Create a single-elimination bracket for the advancing teams,
       with byes if the total is not a power of 2.

    Groups are filled snake-style: A,B,C,D,D,C,B,A,A,...
    so teams are spread evenly when listed alphabetically.
    """
    from .models import Match

    teams = list(division.teams.all().order_by('name'))
    teams = _sort_teams_by_seed(division, teams)
    n = len(teams)
    group_count = max(1, division.group_count)
    advance_count = max(1, division.advance_count)

    if n < group_count * 2:
        return []  # Not enough teams

    # Delete existing matches cleanly
    Match.objects.filter(division=division).delete()

    # ── Divide teams into groups (snake order) ────────────────────────
    groups = [[] for _ in range(group_count)]
    for i, team in enumerate(teams):
        cycle = i // group_count
        pos = i % group_count
        group_idx = pos if cycle % 2 == 0 else (group_count - 1 - pos)
        groups[group_idx].append(team)

    created = []

    # ── Group stage: round-robin per group (circle method) ───────────
    for g_idx, group_teams in enumerate(groups):
        group_num = g_idx + 1
        rounds = _round_robin_rounds(list(group_teams))
        for round_num, pairs in enumerate(rounds, start=1):
            for team1, team2 in pairs:
                match = Match.objects.create(
                    division=division,
                    team1=team1,
                    team2=team2,
                    match_round=round_num,
                    group_number=group_num,
                    phase='group',
                    status='pending',
                )
                created.append(match)

    if not with_bracket:
        return created

    # ── Final phase (knockout or final round-robin, possibly split by gender) ──
    from .final_phase import build_final_phase
    base_round = max((m.match_round for m in created), default=0) + 1
    created += build_final_phase(division, base_round, groups)
    return created


# ---------------------------------------------------------------------------

def generate_schedule(division):
    """Generate schedule based on the division's own tournament_type."""
    if division.tournament_type == 'tree':
        return generate_bracket(division)
    if division.tournament_type == 'playoff':
        return generate_playoff(division)
    if division.tournament_type == 'groups':
        return generate_playoff(division, with_bracket=False)
    return generate_round_robin(division)


def regenerate_playoff_with_groups(division, groups):
    """
    Regenerate a playoff division's group-stage matches using explicit group
    assignments instead of the automatic snake-sort.

    ``groups`` is a list of lists of Team objects (or PKs resolved to teams),
    ordered groups[0] = group 1, groups[1] = group 2, etc.

    Returns the same structure as generate_playoff – all created Match objects.
    """
    from .models import Match

    n = sum(len(g) for g in groups)
    group_count = len(groups)
    advance_count = max(1, division.advance_count)

    if n < group_count * 2:
        return []

    # Delete ALL existing matches (group + playoff placeholders)
    Match.objects.filter(division=division).delete()

    created = []

    # ── Group stage: round-robin per group (circle method) ──────────────
    for g_idx, group_teams in enumerate(groups):
        group_num = g_idx + 1
        rounds = _round_robin_rounds(list(group_teams))
        for round_num, pairs in enumerate(rounds, start=1):
            for team1, team2 in pairs:
                match = Match.objects.create(
                    division=division,
                    team1=team1,
                    team2=team2,
                    match_round=round_num,
                    group_number=group_num,
                    phase='group',
                    status='pending',
                )
                created.append(match)

    if division.tournament_type != 'playoff':
        return created  # 'groups': several round-robins, no playoff bracket

    from .final_phase import build_final_phase
    base_round = max((m.match_round for m in created), default=0) + 1
    created += build_final_phase(division, base_round, [list(g) for g in groups])
    return created
