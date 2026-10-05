"""End of the tournament: medal table, tournament summary and printable diplomas."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _

from .models import Match, Tournament

MEDAL_NAMES = {'gold': "Gold", 'silver': "Silver", 'bronze': "Bronze"}


def tournament_medals(tournament):
    """[{division, podiums: [{label, gold, silver, bronze}]}] for every category, in schedule order."""
    from .views import _division_medal_context
    result = []
    for d in tournament.divisions.prefetch_related('teams__player1', 'teams__player2').order_by('schedule_priority', 'name'):
        ctx = _division_medal_context(d)
        if 'subgroups' in ctx:
            podiums = [{'label': sg['label'], 'gold': sg['gold'], 'silver': sg['silver'], 'bronze': sg['bronze']}
                       for sg in ctx['subgroups']]
        else:
            podiums = [{'label': '', 'gold': ctx['gold'], 'silver': ctx['silver'], 'bronze': ctx['bronze']}]
        podiums = [p for p in podiums if p['gold'] or p['silver'] or p['bronze']]
        result.append({'division': d, 'podiums': podiums,
                       'done': not d.matches.filter(status__in=['pending', 'in_progress']).exists()})
    return result


def tournament_summary(tournament):
    from .public_views import tournament_participants
    players = tournament_participants(tournament)
    matches = Match.objects.filter(division__tournament=tournament)
    completed = matches.filter(status='completed').exclude(team2__isnull=True)
    starts = [m for m in matches.exclude(started_at__isnull=True).values_list('started_at', flat=True)]
    ends = [m for m in matches.exclude(finished_at__isnull=True).values_list('finished_at', flat=True)]
    first, last = (min(starts) if starts else None), (max(ends) if ends else None)
    duration = int((last - first).total_seconds() // 60) if first and last and last > first else None
    played_durations = [
        (f - s).total_seconds() / 60
        for s, f in completed.exclude(started_at__isnull=True).exclude(finished_at__isnull=True)
        .exclude(walkover=True).values_list('started_at', 'finished_at') if f > s
    ]
    return {
        'participants': len(players),
        'girls': sum(1 for p in players if p.gender == 'K'),
        'boys': sum(1 for p in players if p.gender == 'M'),
        'schools': len({p.school for p in players if p.school}),
        'independent': sum(1 for p in players if not p.school),
        'withdrawn': sum(1 for p in players if p.withdrawn),
        'categories': tournament.divisions.count(),
        'matches_total': matches.exclude(team2__isnull=True, bracket_label__isnull=True).count(),
        'matches_played': completed.count(),
        'walkovers': completed.filter(walkover=True).count(),
        'from_courts': completed.exclude(entered_by_court='').count(),
        'first_start': first,
        'last_end': last,
        'duration_min': duration,
        'avg_match_min': round(sum(played_durations) / len(played_durations)) if played_durations else None,
    }


@login_required
def tournament_medal_table(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    return render(request, 'tournaments/medal_table.html', {
        'tournament': tournament,
        'medals': tournament_medals(tournament),
        'summary': tournament_summary(tournament),
    })


@login_required
def tournament_diplomas(request, pk):
    """One A4 landscape diploma per medallist; ?all=1 adds participation diplomas for everyone else."""
    from .public_views import tournament_participants
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    diplomas, awarded = [], set()
    places = {'gold': _("First place"), 'silver': _("Second place"), 'bronze': _("Third place")}
    for entry in tournament_medals(tournament):
        d = entry['division']
        for podium in entry['podiums']:
            for medal in ('gold', 'silver', 'bronze'):
                for team in podium[medal]:
                    for p in team.players:
                        awarded.add(p.pk)
                        diplomas.append({'player': p, 'medal': medal, 'place': places[medal],
                                         'category': d.name, 'subgroup': podium['label']})
    include_all = request.GET.get('all') == '1'
    if include_all:
        for p in tournament_participants(tournament):
            if p.pk not in awarded and not p.withdrawn:
                diplomas.append({'player': p, 'medal': '', 'place': _("For taking part"),
                                 'category': '', 'subgroup': ''})
    return render(request, 'tournaments/diplomas.html', {
        'tournament': tournament,
        'diplomas': diplomas,
        'include_all': include_all,
    })


# ---------------------------------------------------------------------------
# Manual backup of the whole club (players + every tournament), one click
# ---------------------------------------------------------------------------

@login_required
def club_backup(request):
    """ZIP with every tournament of the club (same JSON as "Export backup") and the player list.

    Meant for a manual copy just before and just after a tournament, on top of the
    automatic nightly backup of the server.
    """
    import io
    import json
    import zipfile

    from django.http import HttpResponse
    from django.utils import timezone

    from players.models import Player

    from .views import tournament_export_data

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        players = [
            {'name': p.name, 'age': p.age, 'gender': p.gender, 'division': p.division, 'school': p.school}
            for p in Player.objects.filter(owner=request.user).order_by('name')
        ]
        z.writestr('jugadors.json', json.dumps(players, ensure_ascii=False, indent=2))
        for t in Tournament.objects.filter(owner=request.user).order_by('date'):
            data = tournament_export_data(t)
            safe = ''.join(c if c.isalnum() else '_' for c in t.name)[:40]
            z.writestr(f'tornejos/{t.date}_{safe}_{t.pk}.json', json.dumps(data, ensure_ascii=False, indent=2))
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
    resp = HttpResponse(buf.getvalue(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="copia-tornejos-{stamp}.zip"'
    return resp
