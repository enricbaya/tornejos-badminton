"""Live information for tournament day: delay against the plan and loudspeaker calls."""
from datetime import timedelta
from statistics import median

from django.utils import timezone

from .models import Match

# A call is suggested for the matches planned to start within this many minutes.
CALL_AHEAD_MINUTES = 10
# Delays under this many minutes are shown as "on time".
ON_TIME_MINUTES = 5
# At most this many 'due soon' calls on top of the free courts.
MAX_SOON_CALLS = 2


def compute_delay(tournament, now=None):
    """Minutes the tournament runs behind the plan (0 when on time or not started).

    Combines how late the last matches actually started with how long the next
    planned match is already waiting. Rounded to 5 minutes.
    """
    now = now or timezone.now()
    started = list(
        Match.objects
        .filter(division__tournament=tournament, started_at__isnull=False, scheduled_time__isnull=False)
        .order_by('-started_at')
        .values_list('started_at', 'scheduled_time')[:6]
    )
    if not started:
        return 0
    if started[0][0] < now - timedelta(hours=3):
        return 0  # nothing started recently: not running now
    late = [(s - p).total_seconds() / 60 for s, p in started]
    delay = median(late)
    first_pending = (
        Match.objects
        .filter(division__tournament=tournament, status='pending', scheduled_time__isnull=False)
        .order_by('scheduled_time').values_list('scheduled_time', flat=True).first()
    )
    if first_pending and first_pending < now:
        delay = max(delay, (now - first_pending).total_seconds() / 60)
    delay = max(0, int(5 * round(delay / 5)))
    return delay if delay >= ON_TIME_MINUTES else 0


def apply_estimates(matches, delay):
    """Add .estimated_time to pending matches (scheduled time + current delay)."""
    for m in matches:
        if m.status == 'pending' and m.scheduled_time and delay:
            m.estimated_time = m.scheduled_time + timedelta(minutes=delay)
        else:
            m.estimated_time = None
    return matches


def _court_count_today(tournament, now):
    days = list(tournament.days.all())
    today = [d for d in days if d.date == timezone.localdate(now)]
    return max((d.court_count for d in (today or days)), default=0)


def compute_calls(tournament, now=None):
    """Matches whose players should be called now.

    * one per free court: the next match that can be played (planned for that court first);
    * matches planned to start in the next few minutes.

    A call disappears by itself once its match is started. Each item:
    dict(match, court, reason) with reason 'court_free' or 'soon'.
    """
    from .player_status import get_busy_info

    now = now or timezone.now()
    playing_pks, resting = get_busy_info()
    in_progress = Match.objects.filter(division__tournament=tournament, status='in_progress')
    busy_courts = {str(m.court) for m in in_progress}
    courts = [str(c) for c in range(1, _court_count_today(tournament, now) + 1)]
    free_courts = [c for c in courts if c not in busy_courts]

    pending = list(
        Match.objects
        .filter(division__tournament=tournament, status='pending', team1__isnull=False, team2__isnull=False)
        .select_related('division', 'team1__player1', 'team1__player2', 'team2__player1', 'team2__player2')
        .order_by('scheduled_time', 'match_round', 'match_number', 'pk')[:40]
    )

    def can_play_now(m):
        for t in (m.team1, m.team2):
            for p in t.players:
                if p.withdrawn or p.pk in playing_pks:
                    return False
                if p.pk in resting and resting[p.pk] > now + timedelta(minutes=2):
                    return False
        return True

    ready = [m for m in pending if can_play_now(m)]
    calls, used, called_players = [], set(), set()

    def players_of(m):
        return {p.pk for t in (m.team1, m.team2) for p in t.players}

    def available(m):
        # A player can only be called for one match at a time.
        return m.pk not in used and not (players_of(m) & called_players)

    def take(m, court, reason):
        used.add(m.pk)
        called_players.update(players_of(m))
        calls.append({'match': m, 'court': court, 'reason': reason})

    for court in free_courts:
        here = [m for m in ready if available(m) and str(m.court or '') == court]
        others = [m for m in ready if available(m) and str(m.court or '') not in free_courts]
        pick = (here or others or [None])[0]
        if pick is not None:
            take(pick, court, 'court_free')
    # Matches due soon, taking the current delay into account (at most a few, in time order).
    soon = now + timedelta(minutes=CALL_AHEAD_MINUTES - compute_delay(tournament, now))
    extra = 0
    for m in ready:
        if extra >= MAX_SOON_CALLS:
            break
        if available(m) and m.scheduled_time and m.scheduled_time <= soon:
            take(m, str(m.court or ''), 'soon')
            extra += 1
    return calls
