"""
Read-only public views — no login required.

Lets anonymous visitors select a club and tournament and view the
schedule and standings without any ability to edit data.
"""
from django.db.models import Q
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


@cache_page_with_status(20, page='landing')  # short TTL: tournament list changes rarely, but keep it bounded
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


@cache_page_with_status(10, page='tournament')  # short TTL: results and player status update continuously during play
def public_tournament(request, pk):
    """
    Read-only tournament overview: standings and match results per division.
    """
    tournament = get_object_or_404(Tournament, pk=pk)
    _activate_club_language(request, tournament)
    divisions = tournament.divisions.prefetch_related(
        'teams__player1', 'teams__player2', 'matches__team1', 'matches__team2', 'matches__winner', 'seeds',
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
            .select_related('team1__player1', 'team1__player2', 'team2__player1', 'team2__player2', 'winner')
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
        'participants': tournament_participants(tournament),
    })


def tournament_participants(tournament):
    """All players entered in the tournament, alphabetically."""
    from players.models import Player
    return list(
        Player.objects
        .filter(Q(team_player1__divisions__tournament=tournament) | Q(team_player2__divisions__tournament=tournament))
        .distinct().order_by('name')
    )


@cache_page_with_status(10, page='schedule')  # short TTL: scheduled times/status badges change continuously during play
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
        .select_related('division', 'team1__player1', 'team1__player2', 'team2__player1', 'team2__player2', 'winner')
        .order_by('scheduled_time', 'court')
    )
    seed_lookup = _build_seed_lookup(tournament)
    _apply_seed_labels(matches, seed_lookup)
    playing_pks, resting = get_busy_info()
    apply_status_to_matches(matches, playing_pks, resting)
    now = timezone.now()

    from .live_info import compute_delay
    return render(request, 'tournaments/public/public_schedule.html', {
        'tournament': tournament,
        'matches': matches,
        'now': now,
        'delay': compute_delay(tournament, now),
    })


# ---------------------------------------------------------------------------
# Public player page: everything about one player in one tournament
# ---------------------------------------------------------------------------

from django.db.models import Q  # noqa: E402
from django.http import Http404  # noqa: E402


def _player_position(division, team):
    """(group_number, position, rows) of the team in its group, or (None, position, rows) for one round-robin."""
    if division.tournament_type == 'group':
        rows = compute_standings(division)
        for i, r in enumerate(rows, start=1):
            if r['team'].pk == team.pk:
                return None, i, rows
    elif division.has_groups:
        for g_num, rows in compute_group_standings(division):
            for i, r in enumerate(rows, start=1):
                if r['team'].pk == team.pk:
                    return g_num, i, rows
    return None, None, []


def _match_side(match, team_pks):
    """Return (own_team, opponent, won) for a match seen from the player's side."""
    if match.team1_id in team_pks:
        own, opp = match.team1, match.team2
    else:
        own, opp = match.team2, match.team1
    won = None
    if match.status == 'completed' and match.winner_id:
        won = match.winner_id == (own.pk if own else None)
    return own, opp, won


def _score_from_side(match, own_is_team1):
    if not match.score or own_is_team1:
        return match.score or ''
    parts = []
    for s in match.score.split(','):
        a, _sep, b = s.strip().partition('-')
        parts.append(f'{b.strip()}-{a.strip()}' if b else s.strip())
    return ', '.join(parts)


@cache_page_with_status(10, page='tournament')
def public_player(request, pk, player_pk):
    from players.models import Player
    tournament = get_object_or_404(Tournament, pk=pk)
    _activate_club_language(request, tournament)
    player = get_object_or_404(Player, pk=player_pk, owner=tournament.owner)
    teams = list(player.team_player1.all()) + list(player.team_player2.all())
    team_pks = {t.pk for t in teams}
    divisions = list(tournament.divisions.filter(teams__pk__in=team_pks).distinct().order_by('schedule_priority', 'name'))
    if not divisions:
        raise Http404
    matches = list(
        Match.objects
        .filter(division__tournament=tournament)
        .filter(Q(team1__pk__in=team_pks) | Q(team2__pk__in=team_pks))
        .select_related('division', 'team1__player1', 'team1__player2', 'team2__player1', 'team2__player2', 'winner')
        .order_by('scheduled_time', 'match_round', 'match_number')
    )
    playing_pks, resting = get_busy_info()
    from .player_status import player_status
    status, rest_until = player_status(player.pk, playing_pks, resting)

    upcoming, played = [], []
    won_total = lost_total = 0
    for m in matches:
        own, opp, won = _match_side(m, team_pks)
        m.own, m.opp, m.won = own, opp, won
        m.score_own = _score_from_side(m, m.team1_id in team_pks)
        if m.status == 'completed':
            played.append(m)
            if won is True:
                won_total += 1
            elif won is False:
                lost_total += 1
        else:
            upcoming.append(m)
    played.sort(key=lambda m: m.finished_at or m.scheduled_time or timezone.now(), reverse=True)

    division_info = []
    for d in divisions:
        team = next((t for t in d.teams.all() if t.pk in team_pks), None)
        g_num, pos, rows = _player_position(d, team) if team else (None, None, [])
        division_info.append({'division': d, 'team': team, 'group': g_num, 'position': pos, 'rows': rows})

    from .live_info import apply_estimates, compute_delay
    delay = compute_delay(tournament)
    apply_estimates(upcoming, delay)
    return render(request, 'tournaments/public/player.html', {
        'tournament': tournament,
        'delay': delay,
        'player': player,
        'division_info': division_info,
        'upcoming': upcoming,
        'played': played,
        'won_total': won_total,
        'lost_total': lost_total,
        'status': status,
        'rest_until': rest_until,
    })
