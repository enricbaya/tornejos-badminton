from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.utils.translation import gettext as _
from pathlib import Path
import openpyxl
from .models import Player, Team, DivisionCategory, DEFAULT_DIVISION_CATEGORIES
from .forms import PlayerForm, TeamForm
from tournaments.player_status import get_busy_info, player_status as _player_status

@login_required
def player_list(request):
    """Render the player list with filtering and sorting.

    Args:
        request: Django HTTP request.

    Returns:
        HttpResponse: Rendered player list page.
    """
    division = request.GET.get('division', '')
    gender = request.GET.get('gender', '')
    search = request.GET.get('search', '').strip()
    sort = request.GET.get('sort', 'name')
    direction = request.GET.get('dir', 'asc')

    VALID_SORT_FIELDS = {
        'name': 'name',
        'gender': 'gender',
        'age': 'age',
        'division': 'division',
    }
    sort_field = VALID_SORT_FIELDS.get(sort, 'name')
    if direction == 'desc':
        sort_field = f'-{sort_field}'

    players = list(Player.objects.filter(owner=request.user).order_by(sort_field))
    if division:
        players = [p for p in players if p.division == division]
    if gender:
        players = [p for p in players if p.gender == gender]
    if search:
        players = [p for p in players if search.lower() in p.name.lower()]

    # Annotate each player with their status
    playing_pks, resting = get_busy_info()
    for p in players:
        status, rest_until = _player_status(p.pk, playing_pks, resting)
        p.play_status = status
        p.rest_until_ts = int(rest_until.timestamp()) if rest_until else None

    division_choices = [
        (c, c) for c in
        DivisionCategory.objects.filter(owner=request.user).values_list('name', flat=True)
    ]

    # ── Tier quota ───────────────────────────────────────────────────────────
    player_total = Player.objects.filter(owner=request.user).count()
    try:
        player_limit = request.user.profile.player_limit()
    except Exception:
        player_limit = None
    player_remaining = None if player_limit is None else max(0, player_limit - player_total)

    return render(request, 'players/player_list.html', {
        'players': players,
        'division_choices': division_choices,
        'gender_choices': Player.GENDER_CHOICES,
        'selected_division': division,
        'selected_gender': gender,
        'search': search,
        'sort': sort,
        'dir': direction,
        'player_total': player_total,
        'player_limit': player_limit,
        'player_remaining': player_remaining,
        'col_defs': [
            ('name',     _("Name")),
            ('gender',   _("Gender")),
            ('age',      _("Age")),
            ('division', _("Division")),
            ('',         ''),
        ],
    })

@login_required
def player_template_download(request):
    path = Path(__file__).parent / 'excel' / 'players.xlsx'
    if not path.is_file():
        raise Http404
    return FileResponse(
        open(path, 'rb'),
        as_attachment=True,
        filename='players.xlsx',
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )

# Maps values a user might write in the gender column to the model's M/K codes.
# Danish: M = mand, K = kvinde
# English: M = male, F = female
_GENDER_MAP = {'m': 'M', 'k': 'K', 'f': 'K'}

@login_required
def player_upload(request):
    if request.method != 'POST':
        return redirect('player_list')

    uploaded = request.FILES.get('excel_file')
    if not uploaded:
        messages.error(request, _("No file selected."))
        return redirect('player_list')

    if not uploaded.name.lower().endswith('.xlsx'):
        messages.error(request, _("Please upload an Excel file (.xlsx)."))
        return redirect('player_list')

    try:
        wb = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)
        ws = wb.active
    except Exception:
        messages.error(request, _("Could not read the file. Please upload a valid Excel file (.xlsx)."))
        return redirect('player_list')

    rows = iter(ws.rows)
    raw_headers = next(rows, [])
    headers = [str(c.value).strip().lower() if c.value is not None else '' for c in raw_headers]

    # ── Structural validation: all 4 required columns must be present ──────
    REQUIRED_COLUMNS = {'name', 'age', 'gender', 'division'}
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        wb.close()
        messages.error(
            request,
            _("The spreadsheet is missing required columns: %(cols)s. Expected columns: name, age, gender, division.")
            % {'cols': ', '.join(sorted(missing))},
        )
        return redirect('player_list')

    valid_divisions = set(
        DivisionCategory.objects.filter(owner=request.user).values_list('name', flat=True)
    )

    # Determine how many more players this club is allowed to create
    try:
        tier_limit = request.user.profile.player_limit()
    except Exception:
        tier_limit = None
    if tier_limit is not None:
        current_count = Player.objects.filter(owner=request.user).count()
        limit_remaining = max(0, tier_limit - current_count)
    else:
        limit_remaining = None

    created = 0
    no_division = 0
    limit_blocked = 0
    row_errors = []  # list of already-translated error strings

    for row_num, row in enumerate(rows, start=2):
        vals = {headers[i]: (cell.value if cell.value is not None else '') for i, cell in enumerate(row) if i < len(headers)}

        # Name: required non-empty string
        name = str(vals.get('name', '')).strip()
        if not name:
            row_errors.append(
                _("Row %(n)s: name is required.") % {'n': row_num}
            )
            continue

        # Age: blank → None; non-blank must be an integer between 1 and 100
        raw_age = vals.get('age', '')
        if raw_age == '' or raw_age is None:
            age = None
        else:
            try:
                age = int(raw_age)
                if not (1 <= age <= 100):
                    raise ValueError
            except (ValueError, TypeError):
                row_errors.append(
                    _("Row %(n)s (%(name)s): age must be a whole number between 1 and 100.")
                    % {'n': row_num, 'name': name}
                )
                continue

        # Gender: must be M, K or F (single letter, case-insensitive)
        raw_gender = str(vals.get('gender', '')).strip()
        gender = _GENDER_MAP.get(raw_gender.lower())
        if not gender:
            row_errors.append(
                _("Row %(n)s (%(name)s): gender must be M, K or F.")
                % {'n': row_num, 'name': name}
            )
            continue

        # Division: exact match required; unmatched values are accepted but
        # stored empty (the user can assign a division per-player afterwards).
        raw_division = str(vals.get('division', '')).strip()
        if raw_division in valid_divisions:
            division = raw_division
        else:
            division = ''
            if raw_division:
                no_division += 1

        if limit_remaining is not None:
            if limit_remaining <= 0:
                limit_blocked += 1
                continue
            limit_remaining -= 1

        Player.objects.create(
            name=name,
            age=age,
            gender=gender,
            division=division,
            owner=request.user,
        )
        created += 1

    wb.close()

    parts = []
    if created:
        parts.append(_("%(n)s player(s) added.") % {'n': created})
    if no_division:
        parts.append(_("%(n)s player(s) had an unrecognised division and were added without one.") % {'n': no_division})
    if limit_blocked:
        parts.append(
            _("%(n)s player(s) were not added — the club has reached its player limit. Need more? Contact us for a custom plan.")
            % {'n': limit_blocked}
        )
    if row_errors:
        parts.append(
            _("%(count)s row(s) had errors and were skipped: %(details)s")
            % {'count': len(row_errors), 'details': " | ".join(row_errors)}
        )

    if created:
        if row_errors or no_division or limit_blocked:
            messages.warning(request, " ".join(parts))
        else:
            messages.success(request, " ".join(parts))
    else:
        messages.error(request, " ".join(parts) if parts else _("No players were added."))

    return redirect('player_list')

def _player_limit_reached(user):
    """Return (limit, current_count) if the user has hit their tier limit, else None."""
    try:
        profile = user.profile
    except Exception:
        return None
    limit = profile.player_limit()
    if limit is None:
        return None
    current = Player.objects.filter(owner=user).count()
    if current >= limit:
        return limit, current
    return None


@login_required
def player_add(request):
    blocked = _player_limit_reached(request.user)
    if blocked:
        limit, current = blocked
        messages.error(
            request,
            _("Your club has reached its player limit of %(limit)s players. Need more? Contact us for a custom plan.")
            % {'limit': limit},
        )
        return redirect('player_list')
    if request.method == 'POST':
        form = PlayerForm(request.POST, owner=request.user)
        if form.is_valid():
            player = form.save(commit=False)
            player.owner = request.user
            player.save()
            messages.success(request, _("Player \"%(name)s\" has been created.") % {'name': player.name})
            return redirect('player_list')
    else:
        form = PlayerForm(owner=request.user)
    return render(request, 'players/player_form.html', {'form': form})

@login_required
def player_edit(request, pk):
    """Update an existing player.

    Args:
        request: Django HTTP request.
        pk: Primary key of the player to edit.

    Returns:
        HttpResponse: Player form page or redirect response.
    """
    player = get_object_or_404(Player, pk=pk, owner=request.user)
    if request.method == 'POST':
        form = PlayerForm(request.POST, instance=player, owner=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _("Player \"%(name)s\" has been updated.") % {'name': player.name})
            return redirect('player_list')
    else:
        form = PlayerForm(instance=player, owner=request.user)
    return render(request, 'players/player_form.html', {'form': form, 'player': player})

def _player_blocked_by_tournament(player):
    """Return True if any of the player's teams are registered in a tournament
    (either in a division or have matches), which means the player cannot be deleted."""
    for team in list(player.team_player1.all()) + list(player.team_player2.all()):
        if team.divisions.exists():
            return True
        if team.team1_matches.exists() or team.team2_matches.exists():
            return True
    return False

def _delete_player(player):
    """Delete a player and any teams where they appear as player2 (not cascade-deleted)."""
    player.team_player2.all().delete()
    player.delete()  # team_player1 teams are cascade-deleted automatically

@login_required
def player_delete(request, pk):
    player = get_object_or_404(Player, pk=pk, owner=request.user)
    if _player_blocked_by_tournament(player):
        messages.error(request, _("\"%(name)s\" cannot be deleted because they are registered in a tournament.") % {'name': player.name})
        return redirect('player_list')
    if request.method == 'POST':
        name = player.name
        _delete_player(player)
        messages.success(request, _("Player \"%(name)s\" has been deleted.") % {'name': name})
        return redirect('player_list')
    return render(request, 'players/player_confirm_delete.html', {'object': player, 'type': _('player')})

@login_required
def player_bulk_delete(request):
    if request.method != 'POST':
        return redirect('player_list')
    ids = request.POST.getlist('player_ids')
    if not ids:
        messages.warning(request, _("No players selected."))
        return redirect('player_list')

    players = Player.objects.filter(pk__in=ids, owner=request.user)
    deleted = 0
    blocked_names = []
    for player in players:
        if _player_blocked_by_tournament(player):
            blocked_names.append(player.name)
        else:
            _delete_player(player)
            deleted += 1

    parts = []
    if deleted:
        parts.append(_("%(n)s player(s) deleted.") % {'n': deleted})
    if blocked_names:
        parts.append(
            _("%(n)s player(s) could not be deleted because they are registered in a tournament: %(names)s.")
            % {'n': len(blocked_names), 'names': ', '.join(blocked_names)}
        )

    if blocked_names and not deleted:
        messages.error(request, " ".join(parts))
    elif blocked_names:
        messages.warning(request, " ".join(parts))
    else:
        messages.success(request, " ".join(parts))
    return redirect('player_list')


@login_required
def player_clear_rest(request, pk):
    """Clear ``rest_until`` so a player can be scheduled immediately.

    Args:
        request: Django HTTP request.
        pk: Primary key of the player.

    Returns:
        HttpResponseRedirect: Redirect to the player list.
    """
    if request.method == 'POST':
        player = get_object_or_404(Player, pk=pk, owner=request.user)
        player.rest_until = None
        player.save(update_fields=['rest_until'])
        messages.success(request, _("Rest period for %(name)s has been cleared.") % {'name': player.name})
    return redirect('player_list')

@login_required
def player_toggle_arrived(request, pk):
    """Toggle whether a player has checked in at the judge table.

    Responds with JSON for AJAX callers (so the caller's page doesn't
    reload and lose UI state such as an expanded accordion row).

    Args:
        request: Django HTTP request.
        pk: Primary key of the player.

    Returns:
        HttpResponseRedirect | JsonResponse: Redirect to ``next`` or the
        player list, or a JSON body for AJAX requests.
    """
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    next_url = request.POST.get('next') or ''
    if request.method == 'POST':
        player = get_object_or_404(Player, pk=pk, owner=request.user)
        player.has_arrived = not player.has_arrived
        player.save(update_fields=['has_arrived'])
        if is_ajax:
            return JsonResponse({'ok': True, 'has_arrived': player.has_arrived})
    if next_url:
        return redirect(next_url)
    return redirect('player_list')

@login_required
def team_list(request):
    """Render the team list with filtering and sorting.

    Args:
        request: Django HTTP request.

    Returns:
        HttpResponse: Rendered team list page.
    """
    search = request.GET.get('search', '').strip()
    filter_division = request.GET.get('division', '')
    filter_type = request.GET.get('pair_type', '')
    sort = request.GET.get('sort', 'name')
    direction = request.GET.get('dir', 'asc')

    VALID_SORT_FIELDS = {
        'name': 'name',
        'division': 'division',
        'pair_type': 'pair_type',
    }
    sort_field = VALID_SORT_FIELDS.get(sort, 'name')
    if direction == 'desc':
        sort_field = f'-{sort_field}'

    teams = list(
        Team.objects.filter(player2__isnull=False, player1__owner=request.user)
        .select_related('player1', 'player2')
        .order_by(sort_field)
    )
    if search:
        q = search.lower()
        teams = [t for t in teams if q in (t.name or '').lower()
                 or q in t.player1.name.lower()
                 or (t.player2 and q in t.player2.name.lower())]
    if filter_division:
        teams = [t for t in teams if t.division == filter_division]
    if filter_type:
        teams = [t for t in teams if t.pair_type == filter_type]

    col_defs = [
        ('name',      _("Team")),
        ('pair_type', _("Type")),
        ('division',  _("Division")),
        ('',          _("Player 1")),
        ('',          _("Player 2")),
        ('',          ''),
    ]

    division_choices = [
        (c, c) for c in
        DivisionCategory.objects.filter(owner=request.user).values_list('name', flat=True)
    ]

    return render(request, 'players/team_list.html', {
        'teams': teams,
        'search': search,
        'sort': sort,
        'dir': direction,
        'selected_division': filter_division,
        'selected_type': filter_type,
        'division_choices': division_choices,
        'pair_type_choices': Team.PAIR_TYPE_CHOICES,
        'col_defs': col_defs,
    })

@login_required
def team_add(request):
    """Create a new team for the logged-in user.

    Args:
        request: Django HTTP request.

    Returns:
        HttpResponse: Team form page or redirect response.
    """
    if request.method == 'POST':
        form = TeamForm(request.POST, owner=request.user)
        if form.is_valid():
            team = form.save()
            messages.success(request, _("Team \"%(name)s\" has been created.") % {'name': team.name})
            return redirect('team_list')
    else:
        form = TeamForm(owner=request.user)
    division_choices = [
        (c, c) for c in
        DivisionCategory.objects.filter(owner=request.user).values_list('name', flat=True)
    ]
    return render(request, 'players/team_form.html', {'form': form, 'division_choices': division_choices})

@login_required
def team_edit(request, pk):
    """Update an existing team.

    Args:
        request: Django HTTP request.
        pk: Primary key of the team to edit.

    Returns:
        HttpResponse: Team form page or redirect response.
    """
    team = get_object_or_404(Team, pk=pk, player1__owner=request.user)
    if request.method == 'POST':
        form = TeamForm(request.POST, instance=team, owner=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _("Team \"%(name)s\" has been updated.") % {'name': team.name})
            return redirect('team_list')
    else:
        form = TeamForm(instance=team, owner=request.user)
    division_choices = [
        (c, c) for c in
        DivisionCategory.objects.filter(owner=request.user).values_list('name', flat=True)
    ]
    return render(request, 'players/team_form.html', {'form': form, 'team': team, 'division_choices': division_choices})

@login_required
def team_delete(request, pk):
    """Delete a team after confirmation.

    Args:
        request: Django HTTP request.
        pk: Primary key of the team to delete.

    Returns:
        HttpResponse: Confirmation page or redirect response.
    """
    team = get_object_or_404(Team, pk=pk, player1__owner=request.user)
    if request.method == 'POST':
        name = team.name
        team.delete()
        messages.success(request, _("Team \"%(name)s\" has been deleted.") % {'name': name})
        return redirect('team_list')
    return render(request, 'players/player_confirm_delete.html', {'object': team, 'type': _('pair')})


@login_required
def player_schedule_print(request, pk):
    """Render a printable schedule for one player across tournaments.

    Args:
        request: Django HTTP request.
        pk: Primary key of the player.

    Returns:
        HttpResponse: Rendered printable schedule.
    """
    from tournaments.models import Match, Tournament, DivisionSeed
    player = get_object_or_404(Player, pk=pk, owner=request.user)
    # Find alle teams som spilleren er en del af
    teams = Team.objects.filter(Q(player1=player) | Q(player2=player))
    # Find alle kampe (med tidspunkt) for disse teams, sorteret efter tidspunkt
    matches_qs = (
        Match.objects
        .filter(Q(team1__in=teams) | Q(team2__in=teams))
        .filter(scheduled_time__isnull=False)
        .exclude(match_number=None)
        .distinct()
        .select_related('team1', 'team2', 'division', 'division__tournament', 'winner')
        .order_by('scheduled_time')
    )

    # Collect all tournaments this player has matches in
    all_tournament_ids = list(matches_qs.values_list('division__tournament_id', flat=True).distinct())
    tournaments = list(Tournament.objects.filter(pk__in=all_tournament_ids).order_by('date', 'name'))

    # Apply tournament filter from query string
    selected_tournament_pk = request.GET.get('tournament')
    selected_tournament = None
    if selected_tournament_pk:
        try:
            selected_tournament = next((t for t in tournaments if t.pk == int(selected_tournament_pk)), None)
        except (ValueError, TypeError):
            pass
    if selected_tournament:
        matches_qs = matches_qs.filter(division__tournament=selected_tournament)

    matches = list(matches_qs)

    # Annotate matches with seed display strings
    tournament_ids = set(m.division.tournament_id for m in matches)
    seed_lookup = {
        (s.division_id, s.team_id): s.seed_number
        for s in DivisionSeed.objects.filter(division__tournament_id__in=tournament_ids)
    }
    for match in matches:
        s1 = seed_lookup.get((match.division_id, match.team1_id))
        s2 = seed_lookup.get((match.division_id, match.team2_id))
        match.t1_seed = f' ({s1})' if s1 else ''
        match.t2_seed = f' ({s2})' if s2 else ''

    # Brug logo fra den eneste turnering hvis alle kampe er fra samme turnering
    logo_tournament = None
    if selected_tournament:
        logo_tournament = selected_tournament
    elif len(tournament_ids) == 1:
        logo_tournament = matches[0].division.tournament if matches else None

    return render(request, 'players/player_schedule_print.html', {
        'player': player,
        'matches': matches,
        'logo_tournament': logo_tournament,
        'tournaments': tournaments,
        'selected_tournament': selected_tournament,
    })


@login_required
def division_category_list(request):
    """List and create user-specific division categories.

    Args:
        request: Django HTTP request.

    Returns:
        HttpResponse: Category management page or redirect response.
    """
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            DivisionCategory.objects.get_or_create(owner=request.user, name=name)
            messages.success(request, _("Category \"%(name)s\" has been added.") % {'name': name})
        return redirect('division_category_list')

    categories = DivisionCategory.objects.filter(owner=request.user)
    return render(request, 'players/division_categories.html', {
        'categories': categories,
        'default_categories': DEFAULT_DIVISION_CATEGORIES,
    })


@login_required
def division_category_delete(request, pk):
    """Delete a single user division category.

    Args:
        request: Django HTTP request.
        pk: Primary key of the division category.

    Returns:
        HttpResponseRedirect: Redirect to the category list.
    """
    cat = get_object_or_404(DivisionCategory, pk=pk, owner=request.user)
    if request.method == 'POST':
        cat.delete()
        messages.success(request, _("Category \"%(name)s\" has been deleted.") % {'name': cat.name})
    return redirect('division_category_list')


@login_required
def division_category_seed_defaults(request):
    """Pre-populate default categories for the current user.

    Args:
        request: Django HTTP request.

    Returns:
        HttpResponseRedirect: Redirect to the category list.
    """
    if request.method == 'POST':
        created = 0
        for i, name in enumerate(DEFAULT_DIVISION_CATEGORIES):
            _, was_created = DivisionCategory.objects.get_or_create(
                owner=request.user, name=name,
                defaults={'sort_order': i},
            )
            if was_created:
                created += 1
        if created:
            messages.success(request, _("%(n)s default categories have been added.") % {'n': created})
        else:
            messages.info(request, _("All default categories already exist."))
    return redirect('division_category_list')
