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
# A free court waits for its planned match (instead of advancing another one) when the
# only thing stopping it is a rest that ends within this many minutes.
SHORT_REST_WAIT = 3


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


def _blockers(match, playing, resting, now, court_of_player):
    """Why the match cannot start right now (list of short texts; empty = it can start).

    Players who have not checked in at reception do NOT block a call: calling them is
    precisely how the judge table finds them. They are reported separately.
    """
    from django.utils.translation import gettext as _
    if match.team1 is None or match.team2 is None:
        return [_("waiting for earlier results")]
    out = []
    for t in (match.team1, match.team2):
        for p in t.players:
            if p.withdrawn:
                out.append(_("%(name)s has withdrawn") % {'name': p.name})
            elif p.pk in playing:
                c = court_of_player.get(p.pk)
                out.append(_("%(name)s is playing on court %(court)s") % {'name': p.name, 'court': c}
                           if c else _("%(name)s is playing") % {'name': p.name})
            elif p.pk in resting and resting[p.pk] > now:
                # Same rule as check_match_startable: any remaining rest blocks the start.
                mins = int((resting[p.pk] - now).total_seconds() // 60) + 1
                out.append(_("%(name)s is resting (%(min)s min)") % {'name': p.name, 'min': mins})
    return out


def _rest_wait(match, playing, resting, now):
    """Minutes until every player has rested, when rest is the only blocker; else None."""
    if match.team1 is None or match.team2 is None:
        return None
    wait = 0
    for t in (match.team1, match.team2):
        for p in t.players:
            if p.withdrawn or p.pk in playing:
                return None
            if p.pk in resting and resting[p.pk] > now:
                wait = max(wait, int((resting[p.pk] - now).total_seconds() // 60) + 1)
    return wait or None


def _not_arrived(match):
    if match.team1 is None or match.team2 is None:
        return []
    return [p.name for t in (match.team1, match.team2) for p in t.players if not p.has_arrived]


def court_plan(tournament, now=None):
    """What happens on every court right now. One dict per court, in court order:

    * ``active``   – match being played on the court (or None)
    * ``due``      – next pending match planned for this court in the time schedule
    * ``due_blockers`` – why ``due`` cannot start now ([] when it can)
    * ``proposal`` – for a free court: the match to call now. It is ``due`` when it can be
      played; otherwise the earliest pending match that can be played now (``advanced``).
    * ``advanced`` – True when ``proposal`` is not the match planned for this court
    * ``not_arrived`` – players of the proposal who did not check in at reception

    The loudspeaker panel, the "next per court" list and the court referee page all use
    this, so they always agree on which match is next.
    """
    from .player_status import get_busy_info

    now = now or timezone.now()
    playing, resting = get_busy_info()
    in_progress = list(Match.objects.filter(division__tournament=tournament, status='in_progress')
                       .select_related('division', 'team1__player1', 'team1__player2',
                                       'team2__player1', 'team2__player2'))
    active_by_court = {str(m.court): m for m in in_progress if m.court}
    court_of_player = {}
    for m in in_progress:
        for t in (m.team1, m.team2):
            if t:
                for p in t.players:
                    court_of_player[p.pk] = m.court
    n_courts = _court_count_today(tournament, now) or 4
    for c in active_by_court:
        if c.isdigit():
            n_courts = max(n_courts, int(c))
    pending = list(
        Match.objects
        .filter(division__tournament=tournament, status='pending')
        .select_related('division', 'team1__player1', 'team1__player2', 'team2__player1', 'team2__player2')
        .order_by(models_F_asc('scheduled_time'), 'match_round', 'match_number', 'pk')
    )
    for m in pending:
        m.blockers = _blockers(m, playing, resting, now, court_of_player)
        m.not_arrived = _not_arrived(m)
        # Minutes until the match can start when only rest stops it (None otherwise).
        m.rest_wait = _rest_wait(m, playing, resting, now) if m.blockers else 0

    def players_of(m):
        return {p.pk for t in (m.team1, m.team2) if t for p in t.players}

    plan = []
    for c in range(1, n_courts + 1):
        key = str(c)
        due = next((m for m in pending if str(m.court or '') == key), None)
        plan.append({'court': c, 'active': active_by_court.get(key), 'due': due,
                     'due_blockers': due.blockers if due else [],
                     'proposal': None, 'advanced': False, 'not_arrived': [], 'waiting': False})

    used, called_players = set(), set()

    def take(entry, m, advanced):
        entry['proposal'] = m
        entry['advanced'] = advanced
        entry['not_arrived'] = m.not_arrived
        used.add(m.pk)
        called_players.update(players_of(m))

    free = [e for e in plan if e['active'] is None]
    # 1) free courts whose planned match can be played now
    for e in free:
        d = e['due']
        if d is not None and not d.blockers and not (players_of(d) & called_players):
            take(e, d, False)
    # 2) other free courts: the earliest pending match that can be played now ("advanced")
    for e in free:
        if e['proposal'] is not None:
            continue
        d = e['due']
        if (d is not None and d.rest_wait and d.rest_wait <= SHORT_REST_WAIT
                and not (players_of(d) & called_players)):
            # The planned match can start in a moment: wait for it rather than advancing
            # another one (which would delay the planned match for a whole game).
            e['waiting'] = True
            e['wait_min'] = d.rest_wait
            used.add(d.pk)
            called_players.update(players_of(d))
            continue
        for m in pending:
            if m.pk in used or m.blockers or (players_of(m) & called_players):
                continue
            # do not steal a match that is the planned one of another free court
            planned_court = next((x for x in free if x is not e and x['due'] is m), None)
            if planned_court is not None:
                continue
            take(e, m, True)
            break
    # What to show as "next" on each court: the proposal on a free court; on a busy court the
    # first match planned there that is not already proposed somewhere else.
    for e in plan:
        if e['proposal'] is not None:
            e['next'] = e['proposal']
        elif e['waiting']:
            e['next'] = e['due']
            e['next_blockers'] = e['due'].blockers
        else:
            key = str(e['court'])
            e['next'] = next((m for m in pending if str(m.court or '') == key and m.pk not in used), None)
            e['next_blockers'] = e['next'].blockers if e['next'] is not None else []
    return plan


def models_F_asc(field):
    from django.db.models import F
    return F(field).asc(nulls_last=True)


def compute_calls(tournament, now=None):
    """Loudspeaker calls: one per free court (see ``court_plan``)."""
    calls = []
    for e in court_plan(tournament, now):
        if e['active'] is None and e['proposal'] is not None:
            calls.append({'match': e['proposal'], 'court': str(e['court']), 'reason': 'court_free',
                          'advanced': e['advanced'], 'due': e['due'], 'due_blockers': e['due_blockers'],
                          'not_arrived': e['not_arrived']})
    return calls
