"""Season statistics: per player, per school, and the real duration of the matches.

A season is a school year (September → August). Demo tournaments never count.
"""
import datetime
from collections import defaultdict
from statistics import median

from django.core.cache import cache
from django.db.models import Max, Q
from django.utils.translation import gettext as _

from .models import Match, Tournament

MEDAL_ORDER = {'gold': 0, 'silver': 1, 'bronze': 2}
MEDAL_ICON = {'gold': '🥇', 'silver': '🥈', 'bronze': '🥉'}


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------

def season_start(date):
    return datetime.date(date.year if date.month >= 9 else date.year - 1, 9, 1)


def season_label(date):
    start = season_start(date)
    return f'{start.year}-{str(start.year + 1)[-2:]}'


def season_tournaments(owner, date):
    start = season_start(date)
    end = datetime.date(start.year + 1, 8, 31)
    return list(Tournament.objects.filter(owner=owner, is_demo=False, date__gte=start, date__lte=end)
                .order_by('date', 'pk'))


def available_seasons(owner):
    dates = Tournament.objects.filter(owner=owner, is_demo=False).values_list('date', flat=True)
    seen = {}
    for d in dates:
        seen[season_start(d)] = season_label(d)
    return [{'start': k, 'label': v} for k, v in sorted(seen.items(), reverse=True)]


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

def parse_sets(score):
    """[(a, b), …] from '21-15, 18-21, 21-18'; [] when there is no usable score."""
    from .forms import _parse_score
    if not score:
        return []
    try:
        return _parse_score(score)
    except ValueError:
        return []


def _side_numbers(match, own_is_team1):
    """sets won, sets lost, points won, points lost for one side of a played match."""
    sw = sl = pw = pl = 0
    for a, b in parse_sets(match.score):
        own, opp = (a, b) if own_is_team1 else (b, a)
        pw += own
        pl += opp
        if own > opp:
            sw += 1
        else:
            sl += 1
    return sw, sl, pw, pl


def _empty():
    return {'played': 0, 'won': 0, 'lost': 0, 'wo_won': 0, 'wo_lost': 0,
            'sets_won': 0, 'sets_lost': 0, 'points_won': 0, 'points_lost': 0}


def _add_match(row, match, own_is_team1, won):
    if match.walkover:
        row['wo_won' if won else 'wo_lost'] += 1
        return
    row['played'] += 1
    row['won' if won else 'lost'] += 1
    sw, sl, pw, pl = _side_numbers(match, own_is_team1)
    row['sets_won'] += sw
    row['sets_lost'] += sl
    row['points_won'] += pw
    row['points_lost'] += pl


def _finish(row):
    row['win_pct'] = round(100 * row['won'] / row['played']) if row['played'] else None
    row['point_diff'] = row['points_won'] - row['points_lost']
    return row


# ---------------------------------------------------------------------------
# Medals per tournament (cached: they only change when results change)
# ---------------------------------------------------------------------------

def _fingerprint(tournament):
    agg = Match.objects.filter(division__tournament=tournament).aggregate(
        f=Max('finished_at'), n=Max('pk'))
    done = Match.objects.filter(division__tournament=tournament, status='completed').count()
    import hashlib
    return hashlib.sha1(f"{agg['f']}|{agg['n']}|{done}".encode()).hexdigest()[:16]


def medal_map(tournament):
    """{player_pk: [{'medal', 'category', 'subgroup'}]} for one tournament."""
    key = f'stats-medals-{tournament.pk}-{_fingerprint(tournament)}'
    result = cache.get(key)
    if result is not None:
        return result
    from .final_views import tournament_medals
    result = defaultdict(list)
    for entry in tournament_medals(tournament):
        if not entry['done']:
            continue  # category still being played: no medals yet
        for podium in entry['podiums']:
            for medal in ('gold', 'silver', 'bronze'):
                for team in podium[medal]:
                    for p in team.players:
                        result[p.pk].append({'medal': medal, 'category': entry['division'].name,
                                             'subgroup': podium['label']})
    result = dict(result)
    cache.set(key, result, 3600)
    return result


def _completed_matches(tournaments, extra=None):
    qs = (Match.objects.filter(division__tournament__in=tournaments, status='completed',
                               team1__isnull=False, team2__isnull=False, winner__isnull=False)
          .select_related('division__tournament', 'team1__player1', 'team1__player2',
                          'team2__player1', 'team2__player2'))
    if extra is not None:
        qs = qs.filter(extra)
    return qs


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

def player_season_stats(player, date):
    """Season panel of the public player page."""
    tournaments = season_tournaments(player.owner, date)
    if not tournaments:
        return None
    teams = list(player.team_player1.all()) + list(player.team_player2.all())
    team_pks = {t.pk for t in teams}
    if not team_pks:
        return None
    per_t = {t.pk: dict(_empty(), tournament=t, medals=[]) for t in tournaments}
    total = _empty()
    for m in _completed_matches(tournaments, Q(team1__pk__in=team_pks) | Q(team2__pk__in=team_pks)):
        own_is_team1 = m.team1_id in team_pks
        won = m.winner_id == (m.team1_id if own_is_team1 else m.team2_id)
        _add_match(per_t[m.division.tournament_id], m, own_is_team1, won)
        _add_match(total, m, own_is_team1, won)
    rows = []
    for t in tournaments:
        row = per_t[t.pk]
        row['medals'] = sorted(medal_map(t).get(player.pk, []), key=lambda x: MEDAL_ORDER[x['medal']])
        for md in row['medals']:
            md['icon'] = MEDAL_ICON[md['medal']]
        if row['played'] or row['wo_won'] or row['wo_lost'] or row['medals']:
            rows.append(_finish(row))
    if not rows:
        return None
    total = _finish(total)
    total['tournaments'] = len(rows)
    total['medals'] = {k: sum(1 for r in rows for md in r['medals'] if md['medal'] == k) for k in MEDAL_ORDER}
    return {'season': season_label(date), 'rows': rows, 'total': total,
            'season_tournaments': len(tournaments)}


# ---------------------------------------------------------------------------
# Schools
# ---------------------------------------------------------------------------

def _participants(tournament):
    from .public_views import tournament_participants
    return tournament_participants(tournament)


def school_stats(tournaments):
    """Season table per school: players, participations, matches, wins and medals."""
    schools = defaultdict(lambda: dict(_empty(), players=set(), participations=0, per_tournament={},
                                       gold=0, silver=0, bronze=0))
    school_of = {}
    for t in tournaments:
        for p in _participants(t):
            name = p.school or _("Independent")
            school_of[p.pk] = name
            s = schools[name]
            s['players'].add(p.pk)
            s['participations'] += 1
            s['per_tournament'][t.pk] = s['per_tournament'].get(t.pk, 0) + 1
        for pk, medals in medal_map(t).items():
            if pk in school_of:
                for md in medals:
                    schools[school_of[pk]][md['medal']] += 1
    for m in _completed_matches(tournaments):
        for team, own_is_team1 in ((m.team1, True), (m.team2, False)):
            won = m.winner_id == team.pk
            for p in team.players:
                name = p.school or _("Independent")
                _add_match(schools[name], m, own_is_team1, won)
    rows = []
    for name, s in schools.items():
        s = _finish(s)
        s['name'] = name
        s['player_count'] = len(s.pop('players'))
        s['medal_total'] = s['gold'] + s['silver'] + s['bronze']
        s['tournament_counts'] = [s['per_tournament'].get(t.pk, 0) for t in tournaments]
        rows.append(s)
    rows.sort(key=lambda r: (-r['gold'], -r['silver'], -r['bronze'], -r['won'], r['name']))
    for i, r in enumerate(rows, start=1):
        r['rank'] = i
    return rows


def top_players(tournaments, limit=10):
    """Players with most wins in the season (for the club's statistics page)."""
    stats = defaultdict(_empty)
    players = {}
    for m in _completed_matches(tournaments):
        for team, own_is_team1 in ((m.team1, True), (m.team2, False)):
            won = m.winner_id == team.pk
            for p in team.players:
                players[p.pk] = p
                _add_match(stats[p.pk], m, own_is_team1, won)
    rows = []
    for pk, row in stats.items():
        row = _finish(row)
        row['player'] = players[pk]
        rows.append(row)
    rows.sort(key=lambda r: (-r['won'], -(r['win_pct'] or 0), -r['point_diff'], r['player'].name))
    return rows[:limit]


# ---------------------------------------------------------------------------
# Real duration of the matches
# ---------------------------------------------------------------------------

MIN_REAL, MAX_REAL = 3, 90  # minutes; outside this the clock was not used properly


def _durations(matches):
    out = []
    for m in matches:
        if m.started_at and m.finished_at and not m.walkover:
            mins = (m.finished_at - m.started_at).total_seconds() / 60
            if MIN_REAL <= mins <= MAX_REAL:
                out.append(mins)
    return out


def _summary(values, planned=None):
    if not values:
        return None
    values = sorted(values)
    p80 = values[min(len(values) - 1, int(round(0.8 * (len(values) - 1))))]
    med = median(values)
    return {'n': len(values), 'avg': round(sum(values) / len(values)), 'median': round(med),
            'p80': round(p80), 'min': round(values[0]), 'max': round(values[-1]), 'planned': planned,
            'diff': round(med - planned) if planned else None}


def duration_stats(tournaments):
    """Real duration (start → result) per category and per tournament, against the planned slot."""
    qs = (Match.objects.filter(division__tournament__in=tournaments, status='completed',
                               started_at__isnull=False, finished_at__isnull=False, walkover=False)
          .select_related('division__tournament'))
    by_cat, by_t, by_kind = defaultdict(list), defaultdict(list), defaultdict(list)
    planned_cat = {}
    for m in qs:
        vals = _durations([m])
        if not vals:
            continue
        t = m.division.tournament
        doubles = m.division.discipline != 'single'
        planned = t.double_match_duration if doubles else t.single_match_duration
        by_cat[m.division.name].extend(vals)
        planned_cat.setdefault(m.division.name, planned)
        by_t[t.pk].extend(vals)
        by_kind['double' if doubles else 'single'].extend(vals)
    categories = [dict(_summary(v, planned_cat.get(k)), name=k) for k, v in sorted(by_cat.items())]
    per_tournament = []
    for t in tournaments:
        s = _summary(by_t.get(t.pk, []), t.single_match_duration)
        if s:
            per_tournament.append(dict(s, tournament=t))
    kinds = {k: _summary(v) for k, v in by_kind.items()}
    suggestion = None
    if kinds.get('single'):
        # Slot for planning: what 80 % of the matches need, rounded up to 5 minutes.
        suggestion = 5 * -(-kinds['single']['p80'] // 5)
    return {'categories': categories, 'tournaments': per_tournament, 'kinds': kinds,
            'suggest_single': suggestion}


# ---------------------------------------------------------------------------
# One tournament, per school (printable report for every school)
# ---------------------------------------------------------------------------

def school_report(tournament):
    """[{name, players: [{player, categories, played, won, lost, medals}], totals}] sorted by school."""
    medals = medal_map(tournament)
    players = _participants(tournament)
    stats = defaultdict(_empty)
    for m in _completed_matches([tournament]):
        for team, own_is_team1 in ((m.team1, True), (m.team2, False)):
            won = m.winner_id == team.pk
            for p in team.players:
                _add_match(stats[p.pk], m, own_is_team1, won)
    cats = defaultdict(list)
    for d in tournament.divisions.prefetch_related('teams__player1', 'teams__player2'):
        for team in d.teams.all():
            for p in team.players:
                cats[p.pk].append(d.name)
    schools = defaultdict(list)
    for p in players:
        row = _finish(dict(stats[p.pk]))
        row['player'] = p
        row['categories'] = cats.get(p.pk, [])
        row['medals'] = sorted(medals.get(p.pk, []), key=lambda x: MEDAL_ORDER[x['medal']])
        for md in row['medals']:
            md['icon'] = MEDAL_ICON[md['medal']]
        schools[p.school or _("Independent")].append(row)
    result = []
    for name in sorted(schools, key=lambda n: (n == _("Independent"), n.lower())):
        rows = sorted(schools[name], key=lambda r: (r['categories'][:1], r['player'].name))
        tot = {'players': len(rows), 'played': sum(r['played'] for r in rows), 'won': sum(r['won'] for r in rows),
               'gold': sum(1 for r in rows for md in r['medals'] if md['medal'] == 'gold'),
               'silver': sum(1 for r in rows for md in r['medals'] if md['medal'] == 'silver'),
               'bronze': sum(1 for r in rows for md in r['medals'] if md['medal'] == 'bronze'),
               'withdrawn': sum(1 for r in rows if r['player'].withdrawn)}
        result.append({'name': name, 'rows': rows, 'totals': tot})
    return result
