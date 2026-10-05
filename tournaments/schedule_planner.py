"""
Time-slot scheduling for multi-day tournaments.

Assigns scheduled_time and court to each match using a greedy algorithm.

  Constraints honoured:
    * At most court_count matches run simultaneously per day.
    * A player's matches cannot overlap; consecutive matches must be
      separated by at least player_break_time minutes.
    * Playoff-phase matches cannot start before all group-phase matches
      in the same division (on the same or earlier day) have finished
      (+ one break period).
    * Bracket placeholder matches cannot start before both their feeder
      matches have finished (+ one break period).
    * Higher-priority divisions are scheduled before lower-priority ones.
    * When a division has group_day / playoff_day set, matches are
      constrained to that day's time window.
"""

from datetime import datetime, timedelta, time
from django.utils import timezone

# Granularity of the time grid in minutes.
_SLOT_MINUTES = 5


def _to_slots(minutes):
    """Convert a duration in minutes to discrete time slots (ceiling division)."""
    return (int(minutes) + _SLOT_MINUTES - 1) // _SLOT_MINUTES


# ---------------------------------------------------------------------------
# Day-window helpers
# ---------------------------------------------------------------------------

def _build_day_windows(days):
    """Build list of day-window dicts from an ordered TournamentDay iterable."""
    windows = []
    for d in days:
        naive_start = datetime.combine(d.date, d.start_time)
        start = timezone.make_aware(naive_start) if timezone.is_naive(naive_start) else naive_start
        if d.end_time:
            naive_end = datetime.combine(d.date, d.end_time)
            end = timezone.make_aware(naive_end) if timezone.is_naive(naive_end) else naive_end
        else:
            # Open-ended day: use 23:59:59 as scheduler ceiling
            naive_end = datetime.combine(d.date, time(23, 59, 59))
            end = timezone.make_aware(naive_end) if timezone.is_naive(naive_end) else naive_end
        windows.append({
            'day': d,
            'start': start,
            'end': end,
            'buffer': timedelta(minutes=d.buffer_minutes),
            'court_count': d.court_count,
        })
    return windows


def _get_allowed_windows(match, day_windows):
    """Return the subset of day_windows this match is allowed to be scheduled in."""
    phase = getattr(match, 'phase', 'group')
    if phase == 'group' and match.division.group_day_id:
        filtered = [w for w in day_windows if w['day'].pk == match.division.group_day_id]
        return filtered or day_windows
    elif phase == 'playoff' and match.division.playoff_day_id:
        filtered = [w for w in day_windows if w['day'].pk == match.division.playoff_day_id]
        return filtered or day_windows
    return day_windows


def _find_earliest_slot(allowed_windows, earliest, duration_minutes, court_free):
    """Return (slot_datetime, day_pk, court_num) or (None, None, None) if no fit."""
    duration = timedelta(minutes=duration_minutes)
    best_slot = None
    best_day_pk = None
    best_court = None

    for w in allowed_windows:
        effective_end = w['end'] - w['buffer']
        for c in range(1, w['court_count'] + 1):
            court_key = (w['day'].pk, c)
            slot = max(court_free.get(court_key, w['start']), earliest)
            if slot + duration <= effective_end:
                if best_slot is None or slot < best_slot:
                    best_slot = slot
                    best_day_pk = w['day'].pk
                    best_court = c

    return best_slot, best_day_pk, best_court


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_time_schedule(tournament):
    """
    Assign scheduled_time and court to all numbered matches in *tournament*.

    Returns the number of matches scheduled, or 0 if prerequisites are missing.
    """
    if not tournament.days.exists():
        return 0

    from .models import Match

    matches = list(
        Match.objects
        .filter(division__tournament=tournament)
        .exclude(match_number=None)
        .exclude(walkover=True)
        .order_by('division__schedule_priority', 'match_round', 'division', 'match_number')
        .select_related(
            'division',
            'division__group_day',
            'division__playoff_day',
            'team1__player1', 'team1__player2',
            'team2__player1', 'team2__player2',
        )
    )
    if not matches:
        return 0

    days = list(tournament.days.order_by('date', 'start_time'))
    return _schedule_interleaved(tournament, matches, days)


# ---------------------------------------------------------------------------
# Greedy scheduler
# ---------------------------------------------------------------------------

def _schedule_greedy(tournament, matches, days, start_at=None, busy=None):
    """Multi-day greedy scheduler. Returns number of scheduled matches.

    start_at / busy are used to re-plan only the pending matches in the middle of
    the tournament: nothing is placed before start_at, and ``busy`` gives the
    current state (courts in use, players playing or resting, finished feeders).
    """
    day_windows = _build_day_windows(days)
    if start_at is not None:
        day_windows = [w for w in day_windows if w['end'] > start_at]
        for w in day_windows:
            w['start'] = max(w['start'], start_at)
    if not day_windows:
        return 0
    busy = busy or {}

    # court_free[(day_pk, court_num)] = next free datetime for that court
    court_free = {
        (w['day'].pk, c): max(w['start'], busy.get('courts', {}).get(c, w['start']))
        for w in day_windows
        for c in range(1, w['court_count'] + 1)
    }

    player_free = dict(busy.get('players', {}))              # player_pk → datetime
    bracket_slot_end = dict(busy.get('bracket_slots', {}))   # (div_id, section, round, slot) → datetime
    league_round_end = {}                                     # (div_id, section, round) → datetime
    playoff_group_end = dict(busy.get('group_ends', {}))     # (div_id, day_pk) → datetime
    break_td = timedelta(minutes=tournament.player_break_time)

    current_priority_group = None
    current_group_ends = []
    priority_floor = day_windows[0]['start']

    updated = []
    for match in matches:
        # Update priority floor when priority group changes
        p = match.division.schedule_priority
        if p != current_priority_group:
            if current_group_ends:
                priority_floor = max(priority_floor, min(current_group_ends))
            current_priority_group = p
            current_group_ends = []

        is_placeholder = (match.team1 is None)
        duration_minutes = (
            tournament.single_match_duration
            if match.division.discipline == 'single'
            else tournament.double_match_duration
        )
        player_pks = []

        if is_placeholder:
            r = match.match_round
            s = match.bracket_slot
            div_id = match.division_id
            sec = getattr(match, 'final_section', '')
            if s is None:
                # Final round-robin: one round after the other (players unknown yet).
                prev = league_round_end.get((div_id, sec, r - 1))
                earliest = (prev + break_td) if prev else priority_floor
            else:
                if getattr(match, 'is_third_place', False):
                    s = 1  # fed by the two semi-finals, like the final
                feeder_end_1 = bracket_slot_end.get((div_id, sec, r - 1, 2 * s - 1), priority_floor)
                feeder_end_2 = bracket_slot_end.get((div_id, sec, r - 1, 2 * s), priority_floor)
                earliest = max(feeder_end_1, feeder_end_2) + break_td
        else:
            player_pks = [
                pk for pk in [
                    match.team1.player1_id,
                    getattr(match.team1, 'player2_id', None),
                    match.team2.player1_id if match.team2 else None,
                    getattr(match.team2, 'player2_id', None) if match.team2 else None,
                ]
                if pk
            ]
            if player_pks:
                earliest = max(
                    player_free.get(pk, priority_floor - break_td) + break_td
                    for pk in player_pks
                )
            else:
                earliest = priority_floor

        phase = getattr(match, 'phase', 'group')
        if phase == 'playoff':
            target_day_pk = match.division.playoff_day_id
            if target_day_pk:
                group_end = playoff_group_end.get((match.division_id, target_day_pk), priority_floor)
            else:
                # No assigned day — take the latest group-end across all days
                group_end = max(
                    (v for (div_id, _dpk), v in playoff_group_end.items()
                     if div_id == match.division_id),
                    default=priority_floor,
                )
            earliest = max(earliest, group_end + break_td)

        earliest = max(earliest, priority_floor)

        # Find slot in allowed windows; overflow to all windows if needed
        allowed = _get_allowed_windows(match, day_windows)
        slot, day_pk, court = _find_earliest_slot(allowed, earliest, duration_minutes, court_free)
        if slot is None:
            slot, day_pk, court = _find_earliest_slot(day_windows, earliest, duration_minutes, court_free)
        if slot is None:
            continue  # truly unschedulable — skip

        # Clamp priority_floor to the chosen day's start
        current_window = next((w for w in day_windows if w['day'].pk == day_pk), day_windows[0])
        priority_floor = max(priority_floor, current_window['start'])

        match.scheduled_time = slot
        match.court = str(court)
        updated.append(match)

        end_time = slot + timedelta(minutes=duration_minutes)
        court_free[(day_pk, court)] = end_time  # court available immediately after match

        if not is_placeholder:
            for pk in player_pks:
                player_free[pk] = end_time

        if phase == 'group':
            key = (match.division_id, day_pk)
            playoff_group_end[key] = max(playoff_group_end.get(key, end_time), end_time)

        if match.bracket_slot is not None and not getattr(match, 'is_third_place', False):
            bracket_slot_end[(match.division_id, getattr(match, 'final_section', ''),
                              match.match_round, match.bracket_slot)] = end_time
        if match.bracket_slot is None and match.phase == 'playoff':
            key = (match.division_id, getattr(match, 'final_section', ''), match.match_round)
            league_round_end[key] = max(league_round_end.get(key, end_time), end_time)

        current_group_ends.append(end_time)

    from .models import Match as MatchModel
    MatchModel.objects.bulk_update(updated, ['scheduled_time', 'court'])
    return len(updated)


# ---------------------------------------------------------------------------
# Re-plan the pending matches from now (last-minute changes)
# ---------------------------------------------------------------------------

def replan_pending(tournament, now=None):
    """Give new times and courts to every pending match, starting from now.

    Finished and running matches are kept. Courts in use become free when their
    match is expected to end; players keep their break after the last match.
    Returns the number of re-planned matches.
    """
    from .models import Match

    if not tournament.days.exists():
        return 0
    now = now or timezone.now()
    # Round up to the time grid so the new times look tidy (10:02 → 10:05).
    minute = (now.minute // _SLOT_MINUTES + 1) * _SLOT_MINUTES
    start_at = now.replace(second=0, microsecond=0) + timedelta(minutes=minute - now.minute)
    break_td = timedelta(minutes=tournament.player_break_time)

    def duration(m):
        return tournament.single_match_duration if m.division.discipline == 'single' else tournament.double_match_duration

    courts, players, bracket_slots, group_ends = {}, {}, {}, {}
    days = list(tournament.days.order_by('date', 'start_time'))
    today = next((d for d in days if d.date == timezone.localdate(now)), None)
    done = (
        Match.objects.filter(division__tournament=tournament, status__in=['in_progress', 'completed'])
        .select_related('division', 'team1', 'team2')
    )
    for m in done:
        if m.status == 'in_progress':
            started = m.started_at or now
            end = max(started + timedelta(minutes=duration(m)), start_at)
            if m.court and str(m.court).isdigit():
                courts[int(m.court)] = max(courts.get(int(m.court), start_at), end)
        else:
            end = min(m.finished_at or now, now)
        for t in (m.team1, m.team2):
            for pk in ((t.player1_id, t.player2_id) if t else ()):
                if pk:
                    # The scheduler adds the break itself.
                    players[pk] = max(players.get(pk, end), end)
        if m.bracket_slot is not None and not m.is_third_place:
            bracket_slots[(m.division_id, m.final_section, m.match_round, m.bracket_slot)] = end
        if m.phase == 'group' and today:
            key = (m.division_id, today.pk)
            group_ends[key] = max(group_ends.get(key, end), end)
    # Players who are still resting: never earlier than the end of their rest.
    from players.models import Player
    for p in Player.objects.filter(owner=tournament.owner, rest_until__gt=now).only('pk', 'rest_until'):
        players[p.pk] = max(players.get(p.pk, now), p.rest_until - break_td)

    pending = list(
        Match.objects
        .filter(division__tournament=tournament, status='pending')
        .exclude(match_number=None)
        .exclude(walkover=True)
        .order_by('division__schedule_priority', 'match_round', 'division', 'match_number')
        .select_related('division', 'division__group_day', 'division__playoff_day',
                        'team1__player1', 'team1__player2', 'team2__player1', 'team2__player2')
    )
    if not pending:
        return 0
    busy = {'courts': courts, 'players': players, 'bracket_slots': bracket_slots, 'group_ends': group_ends}
    return _schedule_interleaved(tournament, pending, days, start_at=start_at, busy=busy)


# ---------------------------------------------------------------------------
# Feasibility check (pre-flight)
# ---------------------------------------------------------------------------

def check_schedule_feasibility(tournament):
    """
    Return a list of error strings that block scheduling.
    An empty list means scheduling can proceed.

    NOTE: walk-overs are intentionally NOT excluded — no discount is given.
    """
    from django.utils.translation import gettext as _
    from .models import Match

    if not tournament.days.exists():
        return [_(
            "The tournament has no days configured. "
            "Add at least one day before scheduling."
        )]

    errors = []
    for day in tournament.days.order_by('date', 'start_time'):
        end_t = day.end_time or time(23, 59, 59)
        window_minutes = (
            end_t.hour * 60 + end_t.minute
            - day.start_time.hour * 60 - day.start_time.minute
        )
        effective_per_court = window_minutes - day.buffer_minutes
        if effective_per_court <= 0:
            errors.append(
                _("Day %(n)s: buffer (%(buf)d min.) is greater than or equal to the time window.") % {
                    'n': day.day_number, 'buf': day.buffer_minutes,
                }
            )
            continue

        available_court_minutes = effective_per_court * day.court_count
        required_minutes = 0

        for division in tournament.divisions.filter(group_day=day):
            duration = (
                tournament.single_match_duration
                if division.discipline == 'single'
                else tournament.double_match_duration
            )
            count = Match.objects.filter(
                division=division, phase='group',
            ).exclude(match_number=None).count()
            required_minutes += count * duration

        for division in tournament.divisions.filter(playoff_day=day):
            duration = (
                tournament.single_match_duration
                if division.discipline == 'single'
                else tournament.double_match_duration
            )
            count = Match.objects.filter(
                division=division, phase='playoff',
            ).exclude(match_number=None).count()
            required_minutes += count * duration

        if required_minutes == 0:
            continue

        if required_minutes > available_court_minutes:
            errors.append(
                _(
                    "Day %(n)s (%(date)s): matches require %(req)d court-minutes "
                    "but only %(avail)d are available (incl. %(buf)d min. buffer)."
                ) % {
                    'n': day.day_number,
                    'date': day.date,
                    'req': required_minutes,
                    'avail': int(available_court_minutes),
                    'buf': day.buffer_minutes,
                }
            )

    return errors


# ---------------------------------------------------------------------------
# Interleaved scheduler (default since October 2026)
# ---------------------------------------------------------------------------
#
# The original greedy scheduler placed whole divisions one after the other in
# priority order. With school tournaments this left courts idle: while the
# youngest players rest between rounds, nobody else may use the courts, and the
# last divisions (and their finals) no longer fit in the day.
#
# This one walks through the day in 5-minute steps and, every time a court is
# free, starts the best match that can be played at that moment:
#   1. lowest schedule priority number first (younger divisions keep priority),
#   2. group stage before final phase, earlier rounds first, lower match number.
# A match can be played when its players have finished their previous match
# plus the break, and when what it depends on (whole group stage for the final
# phase, both feeder matches for a knockout round, the previous round of a
# final round-robin) has finished plus the break.

def _schedule_interleaved(tournament, matches, days, start_at=None, busy=None):
    from .models import Match

    day_windows = _build_day_windows(days)
    if start_at is not None:
        day_windows = [w for w in day_windows if w['end'] > start_at]
        for w in day_windows:
            w['start'] = max(w['start'], start_at)
    if not day_windows or not matches:
        return 0
    busy = busy or {}
    break_td = timedelta(minutes=tournament.player_break_time)
    step = timedelta(minutes=_SLOT_MINUTES)

    def duration(m):
        return tournament.single_match_duration if m.division.discipline == 'single' else tournament.double_match_duration

    def players(m):
        out = []
        for t in (m.team1, m.team2):
            if t is not None:
                out += [pk for pk in (t.player1_id, t.player2_id) if pk]
        return out

    pending = list(matches)
    pending_ids = {m.pk for m in pending}
    player_free = dict(busy.get('players', {}))          # pk → end of last match (break added on use)
    slot_end = {}                                          # (div, sec, round, slot) → end
    for (div, sec, r, s), end in busy.get('bracket_slots', {}).items():
        slot_end[(div, sec, r, s)] = end
    league_round_end = {}                                  # (div, sec, round) → end
    group_end = {}                                         # div → end of its group stage so far
    for (div, _day), end in busy.get('group_ends', {}).items():
        group_end[div] = max(group_end.get(div, end), end)

    # Which group matches / knockout matches exist but are not scheduled yet (dependencies)
    div_ids = {m.division_id for m in pending}
    group_left = {}
    for m in pending:
        if m.phase == 'group' and m.division.tournament_type in ('playoff',):
            group_left[m.division_id] = group_left.get(m.division_id, 0) + 1
    ko_index = {}
    for m in Match.objects.filter(division_id__in=div_ids, bracket_slot__isnull=False, is_third_place=False):
        ko_index[(m.division_id, m.final_section, m.match_round, m.bracket_slot)] = m
    # Finals (and 3rd-place matches) of a division split per gender wait until every other
    # final-phase match of that division has a time, so all the finals are played at the end.
    final_round = {}
    for (div, sec, r, _s) in ko_index:
        final_round[(div, sec)] = max(final_round.get((div, sec), 0), r)

    def is_final(m):
        return m.phase == 'playoff' and m.bracket_slot is not None and (
            m.is_third_place or m.match_round == final_round.get((m.division_id, m.final_section)))
    semis_left, semis_end = {}, {}
    for m in pending:
        if m.division.final_split and m.phase == 'playoff' and not is_final(m):
            semis_left[m.division_id] = semis_left.get(m.division_id, 0) + 1
    league_left = {}
    for m in pending:
        if m.bracket_slot is None and m.phase == 'playoff':
            key = (m.division_id, m.final_section, m.match_round)
            league_left[key] = league_left.get(key, 0) + 1

    def ready_at(m):
        """Earliest start allowed by players and dependencies, or None if not ready yet."""
        earliest = day_windows[0]['start']
        for pk in players(m):
            if pk in player_free:
                earliest = max(earliest, player_free[pk] + break_td)
        if m.phase == 'playoff' and m.division.tournament_type == 'playoff':
            if group_left.get(m.division_id):
                return None
            if m.division.final_split and is_final(m):
                if semis_left.get(m.division_id):
                    return None
                if m.division_id in semis_end:
                    earliest = max(earliest, semis_end[m.division_id])
            if m.division_id in group_end:
                earliest = max(earliest, group_end[m.division_id] + break_td)
        if m.team1_id is None or m.team2_id is None:
            if m.bracket_slot is None:
                if m.phase == 'playoff':
                    prev = (m.division_id, m.final_section, m.match_round - 1)
                    if league_left.get(prev):
                        return None
                    if prev in league_round_end:
                        earliest = max(earliest, league_round_end[prev] + break_td)
            else:
                s = 1 if m.is_third_place else m.bracket_slot
                ends = []
                for fs in (2 * s - 1, 2 * s):
                    key = (m.division_id, m.final_section, m.match_round - 1, fs)
                    if key in slot_end:
                        ends.append(slot_end[key])
                        continue
                    feeder = ko_index.get(key)
                    if feeder is not None and feeder.pk in pending_ids:
                        return None   # the feeder match has no time yet
                    # bye, finished before this run or no such slot: only the group stage matters
                if ends:
                    earliest = max(earliest, max(ends) + break_td)
        return earliest

    def sort_key(m):
        return (m.division.schedule_priority, 0 if m.phase == 'group' else 1,
                m.match_round, 1 if m.is_third_place else 0, m.match_number or 0, m.pk)

    pending.sort(key=sort_key)
    updated = []
    for w in day_windows:
        court_free = {c: w['start'] for c in range(1, w['court_count'] + 1)}
        for c, end in busy.get('courts', {}).items():
            if c in court_free:
                court_free[c] = max(court_free[c], end)
        limit = w['end'] - w['buffer']
        t = w['start']
        while t < limit and pending:
            free_courts = [c for c, f in sorted(court_free.items()) if f <= t]
            if free_courts:
                for c in free_courts:
                    chosen = None
                    for m in pending:
                        if w not in _get_allowed_windows(m, day_windows):
                            continue
                        if t + timedelta(minutes=duration(m)) > limit:
                            continue
                        ra = ready_at(m)
                        if ra is not None and ra <= t:
                            chosen = m
                            break
                    if chosen is None:
                        break
                    end = t + timedelta(minutes=duration(chosen))
                    chosen.scheduled_time = t
                    chosen.court = str(c)
                    updated.append(chosen)
                    pending.remove(chosen)
                    pending_ids.discard(chosen.pk)
                    court_free[c] = end
                    for pk in players(chosen):
                        player_free[pk] = end
                    if chosen.phase == 'group':
                        group_end[chosen.division_id] = max(group_end.get(chosen.division_id, end), end)
                        if chosen.division_id in group_left:
                            group_left[chosen.division_id] -= 1
                    if chosen.division.final_split and chosen.phase == 'playoff' and not is_final(chosen):
                        semis_left[chosen.division_id] -= 1
                        semis_end[chosen.division_id] = max(semis_end.get(chosen.division_id, end), end)
                    if chosen.bracket_slot is not None and not chosen.is_third_place:
                        slot_end[(chosen.division_id, chosen.final_section, chosen.match_round, chosen.bracket_slot)] = end
                    if chosen.bracket_slot is None and chosen.phase == 'playoff':
                        key = (chosen.division_id, chosen.final_section, chosen.match_round)
                        league_round_end[key] = max(league_round_end.get(key, end), end)
                        league_left[key] -= 1
            t += step

    Match.objects.bulk_update(updated, ['scheduled_time', 'court'])
    return len(updated)
