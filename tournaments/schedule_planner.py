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
    return _schedule_greedy(tournament, matches, days)


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
    bracket_slot_end = dict(busy.get('bracket_slots', {}))   # (div_id, round, slot) → datetime
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
            feeder_end_1 = bracket_slot_end.get((div_id, r - 1, 2 * s - 1), priority_floor)
            feeder_end_2 = bracket_slot_end.get((div_id, r - 1, 2 * s), priority_floor)
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

        if match.bracket_slot is not None:
            bracket_slot_end[(match.division_id, match.match_round, match.bracket_slot)] = end_time

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
        if m.bracket_slot is not None:
            bracket_slots[(m.division_id, m.match_round, m.bracket_slot)] = end
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
    return _schedule_greedy(tournament, pending, days, start_at=start_at, busy=busy)


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
