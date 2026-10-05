"""Club statistics (season), printable report per school and the demo tournament."""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import stats
from .models import Division, Match, Tournament, TournamentDay


@login_required
def club_stats(request):
    today = timezone.localdate()
    seasons = stats.available_seasons(request.user)
    try:
        ref = datetime.date.fromisoformat(request.GET.get('season', ''))
    except ValueError:
        ref = seasons[0]['start'] if seasons else today
    tournaments = stats.season_tournaments(request.user, ref)
    schools = stats.school_stats(tournaments) if tournaments else []
    return render(request, 'tournaments/club_stats.html', {
        'seasons': seasons,
        'season': stats.season_label(ref),
        'season_start': stats.season_start(ref),
        'tournaments': tournaments,
        'schools': schools,
        'top': stats.top_players(tournaments) if tournaments else [],
        'durations': stats.duration_stats(tournaments) if tournaments else None,
        'totals': {
            'players': sum(s['player_count'] for s in schools),
            'participations': sum(s['participations'] for s in schools),
            'matches': Match.objects.filter(division__tournament__in=tournaments, status='completed',
                                            team2__isnull=False).count(),
            'schools': len(schools),
        },
    })


@login_required
def tournament_school_report(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    report = stats.school_report(tournament)
    only = request.GET.get('school', '')
    return render(request, 'tournaments/school_report_print.html', {
        'tournament': tournament,
        'schools': [s for s in report if not only or s['name'] == only],
        'all_schools': report,
        'only': only,
    })


# ---------------------------------------------------------------------------
# Demo tournament: a small tournament to train the judge table and the referees
# ---------------------------------------------------------------------------

DEMO_PLAYERS = {
    'benjami': [('Pau Demo', 'M', 'Escola Demo A'), ('Jan Demo', 'M', 'Escola Demo B'), ('Nil Demo', 'M', 'Escola Demo A'),
                ('Laia Demo', 'K', 'Escola Demo B'), ('Júlia Demo', 'K', 'Escola Demo A'), ('Martina Demo', 'K', 'Escola Demo B')],
    'alevi': [('Marc Demo', 'M', 'Escola Demo A'), ('Arnau Demo', 'M', 'Escola Demo B'),
              ('Carla Demo', 'K', 'Escola Demo A'), ('Ona Demo', 'K', 'Escola Demo B')],
}


def _delete_demos(owner):
    from players.models import Player, Team
    for t in Tournament.objects.filter(owner=owner, is_demo=True):
        teams = list(Team.objects.filter(divisions__tournament=t).distinct())
        player_pks = {p.pk for team in teams for p in team.players}
        t.delete()
        # Players and teams that only exist for the demo go too.
        for team in teams:
            if not team.divisions.exists():
                team.delete()
        Player.objects.filter(pk__in=player_pks, team_player1__isnull=True, team_player2__isnull=True).delete()


def create_demo_tournament(owner):
    from players.models import Player, Team

    from .court_access import ensure_key
    from .schedule_planner import generate_time_schedule
    from .scheduler import generate_schedule

    _delete_demos(owner)
    now = timezone.localtime()
    start = (now + datetime.timedelta(minutes=5 - now.minute % 5)).replace(second=0, microsecond=0)
    if start.date() != now.date() or start.hour >= 22:
        start = now.replace(hour=21, minute=0, second=0, microsecond=0)  # very late: keep it on today
    last = Tournament.objects.filter(owner=owner, is_demo=False).order_by('-date').first()
    t = Tournament.objects.create(
        name=_("Demo tournament (training)"), date=start.date(), owner=owner, is_demo=True,
        division_model='youth', scoring_model=last.scoring_model if last else 'best_of_5_15',
        single_match_duration=15, double_match_duration=15, player_break_time=5,
        court_access_enabled=True)
    ensure_key(t)
    end = start + datetime.timedelta(hours=3)
    if end.date() != start.date():  # late in the evening: the day cannot go past midnight
        end = start.replace(hour=23, minute=59)
    TournamentDay.objects.create(tournament=t, day_number=1, date=start.date(), start_time=start.time(),
                                 end_time=end.time(), court_count=3, buffer_minutes=0)
    divisions = [
        Division.objects.create(tournament=t, name=_("Demo Under-11"), discipline='single', tournament_type='playoff',
                                group_count=2, advance_count=2, third_place_match=True, schedule_priority=1,
                                bronze_count=1),
        Division.objects.create(tournament=t, name=_("Demo Under-13"), discipline='single', tournament_type='group',
                                group_count=1, schedule_priority=2),
    ]
    for d, key in zip(divisions, ('benjami', 'alevi')):
        teams = []
        for i, (name, gender, school) in enumerate(DEMO_PLAYERS[key]):
            p = Player.objects.create(owner=owner, name=name, gender=gender, school=school,
                                      has_arrived=i % 3 != 2)  # some have not arrived: to practise reception
            teams.append(Team.objects.create(player1=p))
        d.teams.set(teams)
    number = 0
    for d in divisions:
        for m in generate_schedule(d):
            number += 1
            m.match_number = number
            m.save(update_fields=['match_number'])
    generate_time_schedule(t)
    return t


@login_required
@require_POST
def demo_create(request):
    with transaction.atomic():
        t = create_demo_tournament(request.user)
    messages.success(request, _("Demo tournament ready. Practise freely: it does not count in the statistics."))
    return redirect('tournament_run', pk=t.pk)


@login_required
@require_POST
def demo_delete(request):
    with transaction.atomic():
        _delete_demos(request.user)
    messages.success(request, _("Demo tournament deleted."))
    return redirect('tournament_list')
