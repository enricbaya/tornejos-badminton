"""Plan B for tournament day: everything on paper if the connection fails, and a fast way to
enter afterwards the results written on the court sheets."""
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .models import Match, Tournament

_SELECT = ('division', 'team1__player1', 'team1__player2', 'team2__player1', 'team2__player2', 'winner')


@login_required
def tournament_planb(request, pk):
    """Hub page: what to print the morning of the tournament and how to work without connection."""
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    pending = Match.objects.filter(division__tournament=tournament, status__in=['pending', 'in_progress']).count()
    return render(request, 'tournaments/planb.html', {'tournament': tournament, 'pending': pending})


def _court_key(court):
    c = str(court or '')
    return (0, int(c)) if c.isdigit() else (1, c)


@login_required
def tournament_court_sheets(request, pk):
    """One printable sheet per court (and day): its matches in order, with boxes for the sets."""
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    matches = (Match.objects.filter(division__tournament=tournament, court__isnull=False)
               .exclude(court='').exclude(team1__isnull=False, team2__isnull=True)
               .select_related(*_SELECT).order_by('scheduled_time', 'match_number'))
    only_open = request.GET.get('pending') == '1'
    if only_open:
        matches = matches.filter(status__in=['pending', 'in_progress'])
    sheets = OrderedDict()
    for m in matches:
        day = timezone.localtime(m.scheduled_time).date() if m.scheduled_time else None
        sheets.setdefault((day, _court_key(m.court), m.court), []).append(m)
    ordered = [{'day': k[0], 'court': k[2], 'matches': v}
               for k, v in sorted(sheets.items(), key=lambda kv: (kv[0][0] or timezone.localdate(), kv[0][1]))]
    return render(request, 'tournaments/court_sheets_print.html', {
        'tournament': tournament, 'sheets': ordered, 'only_open': only_open,
        'multi_day': len({s['day'] for s in ordered}) > 1,
        'sets': range(1, 4),
    })


@login_required
def tournament_quick_results(request, pk):
    """All the matches without a result, in court and time order, to type in the paper sheets."""
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    court = request.GET.get('court', '')
    qs = (Match.objects.filter(division__tournament=tournament, status__in=['pending', 'in_progress'],
                               team1__isnull=False, team2__isnull=False)
          .select_related(*_SELECT))
    courts = sorted({m.court for m in qs if m.court}, key=_court_key)
    if court:
        qs = qs.filter(court=court)
    matches = sorted(qs, key=lambda m: (m.scheduled_time or timezone.now(), _court_key(m.court), m.match_number or 0))
    points = 15 if tournament.scoring_model == 'best_of_5_15' else 21
    return render(request, 'tournaments/quick_results.html', {
        'tournament': tournament, 'matches': matches, 'courts': courts, 'court': court,
        'points_to_win': points, 'max_points': 21 if points == 15 else 30,
    })


def save_result(match, score, set_rest=True):
    """Validate and store a result exactly like the result window does. Returns error text or ''."""
    from .forms import MatchResultForm, _parse_score
    from .live_ops import apply_withdrawals
    from .player_status import set_player_rest
    from .scheduler import advance_bracket, fill_playoff_bracket_from_group

    try:
        sets = _parse_score(score)
    except ValueError as exc:
        return str(exc)
    t1 = sum(1 for a, b in sets if a > b)
    t2 = sum(1 for a, b in sets if b > a)
    winner = match.team1 if t1 > t2 else match.team2 if t2 > t1 else None
    form = MatchResultForm({'score': score, 'winner': winner.pk if winner else ''}, instance=match)
    if not form.is_valid():
        return ' '.join(str(e) for errs in form.errors.values() for e in errs)
    m = form.save(commit=False)
    m.status = form.cleaned_data.get('status') or 'completed'
    m.save()
    match.refresh_from_db()
    if match.status == 'completed' and set_rest:
        set_player_rest(match)
    advance_bracket(match)
    if match.division.tournament_type == 'playoff' and match.phase == 'group':
        fill_playoff_bracket_from_group(match.division, match.group_number)
    apply_withdrawals(match.division.tournament)
    return ''


@login_required
@require_POST
def match_quick_result(request, pk):
    """AJAX: store the sets typed on the quick entry page (no rest is given: the match was earlier)."""
    match = get_object_or_404(Match, pk=pk, division__tournament__owner=request.user)
    if match.status == 'completed':
        return JsonResponse({'ok': False, 'error': _("This match already has a result.")})
    sets = []
    for i in (1, 2, 3):
        a, b = request.POST.get(f's{i}a', '').strip(), request.POST.get(f's{i}b', '').strip()
        if a == '' and b == '':
            continue
        sets.append(f'{a}-{b}')
    if not sets:
        return JsonResponse({'ok': False, 'error': _("Type the score of the sets.")})
    error = save_result(match, ', '.join(sets), set_rest=False)
    if error:
        return JsonResponse({'ok': False, 'error': error})
    match.refresh_from_db()
    return JsonResponse({'ok': True, 'winner': match.winner.name if match.winner else '', 'score': match.score})
