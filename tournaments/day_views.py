"""Judge-table pages for tournament day: reception (check-in), withdrawals, re-planning,
loudspeaker calls and the delay indicator."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from players.models import Player

from .models import Match, Tournament


def _participants(tournament):
    return list(
        Player.objects
        .filter(Q(team_player1__divisions__tournament=tournament) | Q(team_player2__divisions__tournament=tournament))
        .distinct().order_by('name')
    )


def _safe_next(request, default):
    nxt = request.POST.get('next') or ''
    return nxt if nxt.startswith('/') and not nxt.startswith('//') else default


@login_required
def tournament_reception(request, pk):
    """Check-in desk: every entered player, search, present/absent counters, withdraw."""
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    players = _participants(tournament)
    pks = {p.pk for p in players}
    # Categories and next match per player
    cats, next_match = {}, {}
    for d in tournament.divisions.prefetch_related('teams'):
        for t in d.teams.all():
            for pid in (t.player1_id, t.player2_id):
                if pid in pks:
                    cats.setdefault(pid, []).append(d.name)
    pending = (Match.objects
               .filter(division__tournament=tournament, status__in=['pending', 'in_progress'])
               .exclude(team1__isnull=True).exclude(team2__isnull=True)
               .select_related('team1', 'team2')
               .order_by('scheduled_time', 'match_number'))
    for m in pending:
        for t in (m.team1, m.team2):
            for pid in (t.player1_id, t.player2_id):
                if pid in pks and pid not in next_match:
                    next_match[pid] = m
    for p in players:
        p.categories = cats.get(p.pk, [])
        p.next_match = next_match.get(p.pk)
    present = sum(1 for p in players if p.has_arrived and not p.withdrawn)
    withdrawn = sum(1 for p in players if p.withdrawn)
    return render(request, 'tournaments/reception.html', {
        'tournament': tournament,
        'players': players,
        'present': present,
        'absent': len(players) - present - withdrawn,
        'withdrawn': withdrawn,
    })


@login_required
@require_POST
def tournament_player_withdraw(request, pk, player_pk):
    """The player leaves the tournament: every pending match is lost by walk-over."""
    from .live_ops import withdraw_player
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    player = get_object_or_404(Player, pk=player_pk, owner=request.user)
    with transaction.atomic():
        n = withdraw_player(player, tournament)
    messages.success(request, _("%(name)s has been withdrawn. %(n)s pending match(es) given as walk-over.")
                     % {'name': player.name, 'n': n})
    return redirect(_safe_next(request, f'/tournaments/{tournament.pk}/reception/'))


@login_required
@require_POST
def tournament_player_reinstate(request, pk, player_pk):
    from .live_ops import reinstate_player
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    player = get_object_or_404(Player, pk=player_pk, owner=request.user)
    reinstate_player(player)
    messages.success(request, _("%(name)s is back in the tournament. Walk-overs already given are kept.")
                     % {'name': player.name})
    return redirect(_safe_next(request, f'/tournaments/{tournament.pk}/reception/'))


@login_required
@require_POST
def tournament_replan(request, pk):
    """Give new times and courts to the pending matches, starting now."""
    from .schedule_planner import replan_pending
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    if tournament.schedule_locked:
        messages.error(request, _("The schedule is locked. Unlock it on the schedule page to re-plan."))
    else:
        with transaction.atomic():
            n = replan_pending(tournament)
        if n:
            messages.success(request, _("%(n)s pending matches have new times and courts, starting now.") % {'n': n})
        else:
            messages.warning(request, _("There are no pending matches to re-plan (or the tournament day is over)."))
    return redirect(_safe_next(request, f'/tournaments/{tournament.pk}/run/'))


# ---------------------------------------------------------------------------
# Loudspeaker calls
# ---------------------------------------------------------------------------

@login_required
def tournament_calls(request, pk):
    """HTML fragment with the players to call over the loudspeaker."""
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    return render(request, 'tournaments/partials/_calls.html', live_context(tournament))


@login_required
@require_POST
def match_mark_called(request, pk):
    match = get_object_or_404(Match, pk=pk, division__tournament__owner=request.user)
    match.called_at = None if request.POST.get('undo') == '1' else timezone.now()
    match.save(update_fields=['called_at'])
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'ok': True})
    return redirect('tournament_run', pk=match.division.tournament.pk)


# ---------------------------------------------------------------------------
# Live part of the tournament-run page (courts on top + right column)
# ---------------------------------------------------------------------------

def live_context(tournament):
    """Everything the live parts of the run page need (also used for the first render)."""
    from .live_info import compute_delay, court_plan
    plan = court_plan(tournament)
    active = [e['active'] for e in plan if e['active']]
    known = {str(e['court']) for e in plan}
    # matches in progress on a court number outside today's courts (should not happen, but show them)
    extra = list(Match.objects.filter(division__tournament=tournament, status='in_progress')
                 .exclude(court__in=known).select_related('division', 'team1', 'team2'))
    matches = Match.objects.filter(division__tournament=tournament).exclude(match_number=None) \
        .exclude(team2__isnull=True, score='Bye')
    total = matches.count()
    done = matches.filter(status='completed').count()
    calls = [{'match': e['proposal']} for e in plan if not e['active'] and e['proposal']]
    return {
        'tournament': tournament,
        'plan': plan,
        'calls': calls,
        'any_free': any(not e['active'] for e in plan),
        'delay': compute_delay(tournament),
        'active_matches': active + extra,
        'court_indicators': [{'number': e['court'], 'active_match': e['active']} for e in plan],
        'court_choices': [e['court'] for e in plan],
        'occupied_court_count': sum(1 for e in plan if e['active']),
        'occupied_courts': {str(e['court']) for e in plan if e['active']},
        'total_matches': total,
        'completed_matches': done,
    }


@login_required
def tournament_live(request, pk):
    """JSON with the refreshed HTML of the courts strip and the right column of the run page."""
    from django.template.loader import render_to_string
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    ctx = live_context(tournament)
    return JsonResponse({
        'courts': render_to_string('tournaments/partials/_run_courts.html', ctx, request=request),
        'side': render_to_string('tournaments/partials/_run_side.html', ctx, request=request),
        'occupied': sorted(ctx['occupied_courts']),
    })
