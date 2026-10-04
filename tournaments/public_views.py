"""
Read-only public views — no login required.

Lets anonymous visitors select a club and tournament and view the
schedule and standings without any ability to edit data.
"""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth import get_user_model
from django.utils import timezone, translation
from django.conf import settings as django_settings
from .cache_utils import cache_page_with_status

from .models import Tournament, Match
from .standings import compute_standings, compute_group_standings
from .scheduler import get_bracket_data
from .views import (
    _build_seed_lookup, _apply_seed_labels, _seeds_dict_for_division,
)
from .player_status import get_busy_info, apply_status_to_matches
from .player_status import team_status as _team_status

User = get_user_model()

LANGUAGE_COOKIE = getattr(django_settings, 'LANGUAGE_COOKIE_NAME', 'django_language')


def _activate_club_language(request, tournament):
    """If the visitor hasn't chosen a language via the switcher, use the club's default."""
    if LANGUAGE_COOKIE in request.COOKIES:
        return  # visitor has already chosen explicitly
    try:
        lang = tournament.owner.profile.language
        if lang:
            translation.activate(lang)
    except Exception:
        pass


@cache_page_with_status(20)  # short TTL: tournament list changes rarely, but keep it bounded
def public_landing(request):
    """
    Landing page: choose a club (owner) and then a tournament.
    All tournaments with an owner are listed; JS filters by club.
    """
    clubs = (
        User.objects
        .filter(tournaments__isnull=False)
        .distinct()
        .order_by('username')
    )
    today = timezone.localdate()
    tournaments = (
        Tournament.objects
        .filter(owner__isnull=False)
        .select_related('owner')
        .order_by('owner__username', '-date', 'name')
    )
    upcoming = [t for t in tournaments if t.date >= today]
    past = [t for t in tournaments if t.date < today]
    return render(request, 'tournaments/public/landing.html', {
        'clubs': clubs,
        'tournaments': tournaments,
        'upcoming': upcoming,
        'past': past,
    })


@cache_page_with_status(10)  # short TTL: results and player status update continuously during play
def public_tournament(request, pk):
    """
    Read-only tournament overview: standings and match results per division.
    """
    tournament = get_object_or_404(Tournament, pk=pk)
    _activate_club_language(request, tournament)
    divisions = tournament.divisions.prefetch_related(
        'teams', 'matches__team1', 'matches__team2', 'matches__winner', 'seeds',
    )
    seed_lookup = _build_seed_lookup(tournament)
    playing_pks, resting = get_busy_info()

    division_data = []
    for d in divisions:
        standings = compute_standings(d) if d.tournament_type == 'group' else []
        group_standings = compute_group_standings(d) if d.has_groups else []
        bracket_data = get_bracket_data(d) if d.tournament_type in ('tree', 'playoff') else None

        # Annotate matches with seed labels + player status
        matches = list(
            d.matches
            .exclude(match_number=None)
            .select_related('team1', 'team2', 'winner')
            .order_by('match_round', 'match_number')
        )
        _apply_seed_labels(matches, seed_lookup)
        apply_status_to_matches(matches, playing_pks, resting)

        # Annotate standing rows with status
        for row in standings:
            s, ru = _team_status(row['team'], playing_pks, resting)
            row['status'] = s
            row['rest_until'] = ru
        for _gnum, g_rows in group_standings:
            for row in g_rows:
                s, ru = _team_status(row['team'], playing_pks, resting)
                row['status'] = s
                row['rest_until'] = ru

        division_data.append({
            'division': d,
            'standings': standings,
            'group_standings': group_standings,
            'bracket_data': bracket_data,
            'matches': matches,
            'seeds_dict': _seeds_dict_for_division(d, seed_lookup),
        })

    return render(request, 'tournaments/public/tournament.html', {
        'tournament': tournament,
        'division_data': division_data,
    })


@cache_page_with_status(10)  # short TTL: scheduled times/status badges change continuously during play
def public_schedule(request, pk):
    """
    Read-only schedule view — same data as the admin schedule but without
    generate/lock controls.
    """
    tournament = get_object_or_404(Tournament, pk=pk)
    _activate_club_language(request, tournament)
    matches = list(
        Match.objects
        .filter(division__tournament=tournament, scheduled_time__isnull=False)
        .select_related('division', 'team1', 'team2', 'winner')
        .order_by('scheduled_time', 'court')
    )
    seed_lookup = _build_seed_lookup(tournament)
    _apply_seed_labels(matches, seed_lookup)
    playing_pks, resting = get_busy_info()
    apply_status_to_matches(matches, playing_pks, resting)
    now = timezone.now()

    return render(request, 'tournaments/public/public_schedule.html', {
        'tournament': tournament,
        'matches': matches,
        'now': now,
    })
