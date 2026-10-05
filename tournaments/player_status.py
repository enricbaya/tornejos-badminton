"""
Player status helpers: detect whether a player is currently playing or resting.

Status values:
  'playing'  – player's team has an in_progress match right now
  'resting'  – player.rest_until is in the future (post-match break)
  None       – player is free
"""
from django.utils import timezone
from django.utils.translation import gettext as _


# ---------------------------------------------------------------------------
# Core lookups
# ---------------------------------------------------------------------------

def get_busy_info():
    """
    Return (playing_pks, resting) for ALL players globally.

      playing_pks  – set of player PKs with an in_progress match
      resting      – dict { player_pk: rest_until_datetime }  (only future entries)
    """
    from tournaments.models import Match
    from players.models import Player

    now = timezone.now()

    # Playing: teams involved in any in_progress match
    in_progress = Match.objects.filter(status='in_progress').select_related(
        'team1__player1', 'team1__player2',
        'team2__player1', 'team2__player2',
    )
    playing_pks = set()
    for m in in_progress:
        for team in (m.team1, m.team2):
            if team:
                if team.player1_id:
                    playing_pks.add(team.player1_id)
                if team.player2_id:
                    playing_pks.add(team.player2_id)

    # Resting: players whose rest_until hasn't expired yet
    resting = {
        p.pk: p.rest_until
        for p in Player.objects.filter(rest_until__gt=now).only('pk', 'rest_until')
    }

    return playing_pks, resting


# ---------------------------------------------------------------------------
# Per-entity status
# ---------------------------------------------------------------------------

def player_status(player_pk, playing_pks, resting):
    """Return ('playing', None) | ('resting', rest_until) | (None, None)."""
    if player_pk in playing_pks:
        return 'playing', None
    if player_pk in resting:
        return 'resting', resting[player_pk]
    return None, None


def team_status(team, playing_pks, resting):
    """Return ('playing', None) | ('resting', rest_until) | (None, None) for a team."""
    if team is None:
        return None, None
    pks = [pk for pk in (team.player1_id, team.player2_id) if pk]
    for pk in pks:
        if pk in playing_pks:
            return 'playing', None
    latest = None
    for pk in pks:
        ru = resting.get(pk)
        if ru and (latest is None or ru > latest):
            latest = ru
    if latest:
        return 'resting', latest
    return None, None


# ---------------------------------------------------------------------------
# Annotate matches
# ---------------------------------------------------------------------------

def apply_status_to_matches(matches, playing_pks, resting):
    """Annotate match objects with .t1_status, .t1_rest_until, .t2_status, .t2_rest_until."""
    for match in matches:
        s1, r1 = team_status(match.team1, playing_pks, resting)
        s2, r2 = team_status(match.team2, playing_pks, resting)
        match.t1_status = s1
        match.t1_rest_until = r1
        match.t2_status = s2
        match.t2_rest_until = r2


# ---------------------------------------------------------------------------
# Set rest after a match completes
# ---------------------------------------------------------------------------

def set_player_rest(match):
    """
    Called when a match transitions to 'completed'.
    Sets rest_until = now + player_break_time for all players in the match.
    """
    from datetime import timedelta
    now = timezone.now()
    break_td = timedelta(minutes=match.division.tournament.player_break_time)
    rest_until = now + break_td
    players = []
    for team in (match.team1, match.team2):
        if not team:
            continue
        if team.player1:
            players.append(team.player1)
        if team.player2:
            players.append(team.player2)
    for p in players:
        p.rest_until = rest_until
        p.save(update_fields=['rest_until'])


# ---------------------------------------------------------------------------
# Guard: can a match start?
# ---------------------------------------------------------------------------

def check_match_startable(match, ignore_rest=False):
    """
    Return a list of human-readable error strings if any player is busy.
    Empty list means the match can start.

    ``ignore_rest``: the judge table decided to start although a player has not finished
    the set rest (e.g. the player says they are ready). Playing / not arrived still block.
    """
    playing_pks, resting = get_busy_info()
    now = timezone.now()
    errors = []
    for team in (match.team1, match.team2):
        if not team:
            continue
        for player in (team.player1, team.player2):
            if not player:
                continue
            if player.pk in playing_pks:
                errors.append(_('%(name)s is already playing a match') % {'name': player.name})
            elif player.pk in resting and resting[player.pk] > now and not ignore_rest:
                remaining = int((resting[player.pk] - now).total_seconds() // 60) + 1
                errors.append(_('%(name)s is still resting (approx. %(min)s min)') % {'name': player.name, 'min': remaining})
            elif not player.has_arrived:
                errors.append(_('%(name)s has not arrived yet') % {'name': player.name})
    return errors
