"""Last-minute changes on tournament day: walk-overs, withdrawn players."""
from django.db import transaction

from .models import Match
from .scheduler import advance_bracket, fill_playoff_bracket_from_group


def walkover_score(match, winner):
    """15-0, 15-0 (or 21-0) written from team 1's side."""
    pts = 15 if match.division.tournament.scoring_model == 'best_of_5_15' else 21
    if winner is not None and winner.pk == match.team2_id:
        return f'0-{pts}, 0-{pts}'
    return f'{pts}-0, {pts}-0'


def after_result(match):
    """Bracket and group follow-up once a match is completed."""
    advance_bracket(match)
    if match.division.tournament_type == 'playoff' and match.phase == 'group':
        fill_playoff_bracket_from_group(match.division, match.group_number)


def record_walkover(match, winner, court=''):
    """Complete a match as walk-over for ``winner`` (the player who showed up)."""
    match.winner = winner
    match.score = walkover_score(match, winner)
    match.status = 'completed'
    match.walkover = True
    if court:
        match.entered_by_court = str(court)
    match.save()
    after_result(match)


def _team_withdrawn(team):
    return bool(team) and any(p.withdrawn for p in team.players)


def apply_withdrawals(tournament):
    """Give a walk-over to the opponent of a withdrawn player in every pending match
    where both sides are known. Repeats while new matches get filled by the bracket.
    Returns the number of walk-overs recorded."""
    total = 0
    for _round in range(20):
        found = 0
        qs = (Match.objects
              .filter(division__tournament=tournament, status__in=['pending', 'in_progress'],
                      team1__isnull=False, team2__isnull=False)
              .select_related('division__tournament', 'team1__player1', 'team1__player2',
                              'team2__player1', 'team2__player2'))
        for m in qs:
            out1, out2 = _team_withdrawn(m.team1), _team_withdrawn(m.team2)
            if out1 == out2:
                continue  # nobody withdrawn, or both: the judge table decides
            with transaction.atomic():
                record_walkover(m, m.team2 if out1 else m.team1)
            found += 1
        total += found
        if not found:
            break
    return total


def withdraw_player(player, tournament):
    """Mark the player as withdrawn and lose all their pending matches by walk-over."""
    player.withdrawn = True
    player.has_arrived = False
    player.save(update_fields=['withdrawn', 'has_arrived'])
    return apply_withdrawals(tournament)


def reinstate_player(player):
    """Undo a withdrawal for the matches still to come (walk-overs already given stay)."""
    player.withdrawn = False
    player.save(update_fields=['withdrawn'])
