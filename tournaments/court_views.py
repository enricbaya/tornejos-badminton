"""Phone pages for court referees (no login: access through a signed court link)."""
from django.contrib import messages
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache

from .court_access import court_count, signature_is_valid
from .forms import MatchResultForm, _parse_score
from .models import Match, Tournament
from .player_status import apply_status_to_matches, check_match_startable, get_busy_info, set_player_rest
from .public_views import _activate_club_language
from .scheduler import advance_bracket, fill_playoff_bracket_from_group

# How many upcoming matches the referee can choose from.
CHOICES_SHOWN = 4


def _get_court(request, tournament_pk, court, sig):
    """Resolve and authorise a court link, or raise 404 (same answer for every failure)."""
    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    if not tournament.court_access_enabled or not (1 <= court <= court_count(tournament)) \
            or not signature_is_valid(tournament, court, sig):
        raise Http404(_("This court link is not valid or has been switched off."))
    _activate_club_language(request, tournament)
    return tournament


def _panel_redirect(tournament, court, sig):
    return redirect('court_panel', tournament_pk=tournament.pk, court=court, sig=sig)


def _upcoming(tournament, playing_pks, resting):
    """Pending matches with both players known: scheduled ones first (in time order), then the rest."""
    matches = list(
        Match.objects
        .filter(division__tournament=tournament, status='pending',
                team1__isnull=False, team2__isnull=False)
        .select_related('division', 'team1', 'team1__player1', 'team1__player2',
                        'team2', 'team2__player1', 'team2__player2')
        .order_by(F('scheduled_time').asc(nulls_last=True), 'match_round', 'match_number', 'pk')[:30]
    )
    apply_status_to_matches(matches, playing_pks, resting)
    return [m for m in matches if m.t1_status != 'playing' and m.t2_status != 'playing']


def _match_players(match):
    return [p for t in (match.team1, match.team2) if t for p in (t.player1, t.player2) if p]


def _only_arrival_blockers(match):
    """True when the match is only blocked because some players have not checked in."""
    from .player_status import get_busy_info as _busy
    playing_pks, resting = _busy()
    now = timezone.now()
    for p in _match_players(match):
        if p.pk in playing_pks or (p.pk in resting and resting[p.pk] > now):
            return False
    return True


@never_cache
def court_panel(request, tournament_pk, court, sig):
    tournament = _get_court(request, tournament_pk, court, sig)
    active = (
        Match.objects
        .filter(division__tournament=tournament, status='in_progress', court=str(court))
        .select_related('division', 'team1', 'team2')
        .first()
    )
    choices = []
    draft = request.session.pop(f'court_draft_{active.pk}', None) if active else None
    plan_entry = None
    if not active:
        from .live_info import court_plan
        plan_entry = next((e for e in court_plan(tournament) if e['court'] == court), None)
        proposal = plan_entry['proposal'] if plan_entry else None
        playing_pks, resting = get_busy_info()
        for m in _upcoming(tournament, playing_pks, resting):
            if proposal is not None and m.pk == proposal.pk:
                continue
            m.blockers = check_match_startable(m)
            # Only "not arrived yet" blockers can be solved by the referee on court.
            m.only_arrival = bool(m.blockers) and _only_arrival_blockers(m)
            m.is_suggested = False
            choices.append(m)
            if len(choices) >= CHOICES_SHOWN - 1:
                break
        if proposal is not None:
            proposal.blockers = check_match_startable(proposal)
            proposal.only_arrival = bool(proposal.blockers) and _only_arrival_blockers(proposal)
            proposal.is_suggested = True
            proposal.is_advanced = plan_entry['advanced']
            choices.insert(0, proposal)
    points = 15 if tournament.scoring_model == 'best_of_5_15' else 21
    return render(request, 'tournaments/court_panel.html', {
        'tournament': tournament,
        'court': court,
        'sig': sig,
        'active': active,
        'choices': choices,
        'points_to_win': points,
        'max_points': 21 if points == 15 else 30,
        'court_total': court_count(tournament),
        'draft': draft or {},
        'plan': plan_entry,
    })


def court_start(request, tournament_pk, court, sig, match_pk):
    tournament = _get_court(request, tournament_pk, court, sig)
    if request.method != 'POST':
        return _panel_redirect(tournament, court, sig)
    with transaction.atomic():
        match = get_object_or_404(
            Match.objects.select_for_update(), pk=match_pk, division__tournament=tournament)
        busy_court = Match.objects.filter(
            division__tournament=tournament, status='in_progress', court=str(court)).exclude(pk=match.pk)
        if match.status != 'pending' or not match.team1_id or not match.team2_id:
            messages.error(request, _("This match can no longer be started (it may have been started on another court)."))
        elif busy_court.exists():
            messages.error(request, _("Court %(court)s is already in use.") % {'court': court})
        else:
            if request.POST.get('mark_arrived') == '1':
                # The referee sees the players on court: check them in.
                for p in _match_players(match):
                    if not p.has_arrived:
                        p.has_arrived = True
                        p.save(update_fields=['has_arrived'])
            errors = check_match_startable(match)
            if errors:
                messages.error(request, _("Cannot start match: ") + ' · '.join(errors))
            else:
                match.status = 'in_progress'
                match.court = str(court)
                match.started_at = timezone.now()
                match.save(update_fields=['status', 'court', 'started_at'])
                messages.success(request, _("Match #%(n)s is now in progress.") % {'n': match.match_number or match.pk})
    return _panel_redirect(tournament, court, sig)


def court_cancel(request, tournament_pk, court, sig, match_pk):
    """Undo a start (wrong match started): the match goes back to the queue."""
    tournament = _get_court(request, tournament_pk, court, sig)
    if request.method == 'POST':
        match = get_object_or_404(Match, pk=match_pk, division__tournament=tournament,
                                  status='in_progress', court=str(court))
        match.status = 'pending'
        match.started_at = None
        match.save(update_fields=['status', 'started_at'])
        messages.success(request, _("Match #%(n)s has been reset to pending.") % {'n': match.match_number or match.pk})
    return _panel_redirect(tournament, court, sig)


def court_result(request, tournament_pk, court, sig, match_pk):
    tournament = _get_court(request, tournament_pk, court, sig)
    if request.method != 'POST':
        return _panel_redirect(tournament, court, sig)
    match = get_object_or_404(Match, pk=match_pk, division__tournament=tournament,
                              status='in_progress', court=str(court))
    sets = []
    for i in (1, 2, 3):
        a, b = request.POST.get(f's{i}a', '').strip(), request.POST.get(f's{i}b', '').strip()
        if a == '' and b == '':
            continue
        sets.append(f'{a}-{b}')
    score = ', '.join(sets)
    winner = None
    try:
        parsed = _parse_score(score) if score else []
        t1 = sum(1 for a, b in parsed if a > b)
        t2 = sum(1 for a, b in parsed if b > a)
        if t1 != t2:
            winner = match.team1 if t1 > t2 else match.team2
    except ValueError:
        pass
    form = MatchResultForm({'score': score, 'winner': winner.pk if winner else ''}, instance=match)
    if not score:
        messages.error(request, _("Enter the score of each set."))
    elif form.is_valid():
        m = form.save(commit=False)
        m.status = 'completed'
        m.entered_by_court = str(court)
        m.save()
        match.refresh_from_db()
        set_player_rest(match)
        advance_bracket(match)
        if match.division.tournament_type == 'playoff' and match.phase == 'group':
            fill_playoff_bracket_from_group(match.division, match.group_number)
        from .live_ops import apply_withdrawals
        apply_withdrawals(tournament)
        messages.success(request, _("Result saved. Court %(court)s is free.") % {'court': court})
        return _panel_redirect(tournament, court, sig)
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    # Keep what the referee typed so they only have to fix the wrong set.
    request.session[f'court_draft_{match.pk}'] = {k: request.POST.get(k, '') for k in
                                                   ('s1a', 's1b', 's2a', 's2b', 's3a', 's3b')}
    return _panel_redirect(tournament, court, sig)


def court_walkover(request, tournament_pk, court, sig, match_pk):
    """A player did not show up: the opponent wins 15-0, 15-0 and the court is free."""
    tournament = _get_court(request, tournament_pk, court, sig)
    if request.method != 'POST':
        return _panel_redirect(tournament, court, sig)
    from .live_ops import apply_withdrawals, record_walkover
    with transaction.atomic():
        match = get_object_or_404(Match.objects.select_for_update(), pk=match_pk, division__tournament=tournament,
                                  status__in=['pending', 'in_progress'])
        if match.status == 'in_progress' and str(match.court) != str(court):
            raise Http404
        winner = {str(match.team1_id): match.team1, str(match.team2_id): match.team2}.get(request.POST.get('winner', ''))
        if not winner or not match.team1_id or not match.team2_id:
            messages.error(request, _("Choose the player who showed up."))
            return _panel_redirect(tournament, court, sig)
        for p in winner.players:
            if not p.has_arrived:
                p.has_arrived = True
                p.save(update_fields=['has_arrived'])
        record_walkover(match, winner, court=court)
    apply_withdrawals(tournament)
    messages.success(request, _("Walk-over recorded – %(winner)s wins.") % {'winner': winner.name})
    return _panel_redirect(tournament, court, sig)


# ---------------------------------------------------------------------------
# Judge table: manage the court links
# ---------------------------------------------------------------------------

from django.contrib.auth.decorators import login_required  # noqa: E402

from .court_access import court_url, ensure_key, qr_svg, regenerate_key  # noqa: E402


@login_required
def tournament_court_links(request, pk):
    """Court links and QR codes for the referees (judge table only)."""
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    ensure_key(tournament)
    courts = []
    for c in range(1, court_count(tournament) + 1):
        url = court_url(request, tournament, c)
        active = Match.objects.filter(division__tournament=tournament, status='in_progress', court=str(c)) \
            .select_related('team1', 'team2').first()
        courts.append({'number': c, 'url': url, 'qr': qr_svg(url), 'active': active})
    entered = Match.objects.filter(division__tournament=tournament).exclude(entered_by_court='').count()
    return render(request, 'tournaments/court_links.html', {
        'tournament': tournament,
        'courts': courts,
        'entered_by_courts': entered,
    })


@login_required
def court_access_toggle(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    if request.method == 'POST':
        ensure_key(tournament)
        tournament.court_access_enabled = not tournament.court_access_enabled
        tournament.save(update_fields=['court_access_enabled'])
        if tournament.court_access_enabled:
            messages.success(request, _("Court referees are enabled: the court links and QR codes work."))
        else:
            messages.success(request, _("Court referees are disabled: the court links no longer work."))
    return redirect('tournament_court_links', pk=tournament.pk)


@login_required
def court_access_regenerate(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    if request.method == 'POST':
        regenerate_key(tournament)
        messages.success(request, _("New court links created. The old QR codes no longer work: print the court signs again."))
    return redirect('tournament_court_links', pk=tournament.pk)
