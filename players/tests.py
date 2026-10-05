from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from .models import Player, Team

User = get_user_model()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(username='testclub', password='testpass123'):
    return User.objects.create_user(username=username, password=password)


def make_player(name="Alice", age=20, division="A", gender="K", owner=None):
    return Player.objects.create(name=name, age=age, division=division, gender=gender, owner=owner)


def make_team(p1=None, p2=None):
    p1 = p1 or make_player("Alice")
    p2 = p2 or make_player("Bob")
    return Team.objects.create(player1=p1, player2=p2)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class PlayerModelTest(TestCase):
    def test_str_includes_name_and_division(self):
        player = make_player(name="Alice", division="U13")
        self.assertIn("Alice", str(player))
        self.assertIn("U13", str(player))

    def test_gender_choices(self):
        m = make_player(name="Bob", gender="M")
        k = make_player(name="Alice", gender="K")
        self.assertEqual(m.get_gender_display(), "Male")
        self.assertEqual(k.get_gender_display(), "Female")

    def test_valid_division_choices(self):
        valid_divisions = ["U9", "U11", "U13", "U15", "U17", "U19", "A", "B", "C"]
        for div in valid_divisions:
            p = make_player(division=div)
            self.assertEqual(p.division, div)


class TeamModelTest(TestCase):
    def test_auto_name_from_players(self):
        p1 = make_player("Alice")
        p2 = make_player("Bob")
        team = Team.objects.create(player1=p1, player2=p2)
        self.assertEqual(team.name, "Alice & Bob")

    def test_custom_name_is_preserved(self):
        p1 = make_player("Alice")
        p2 = make_player("Bob")
        team = Team.objects.create(player1=p1, player2=p2, name="Dream Team")
        self.assertEqual(team.name, "Dream Team")

    def test_str_returns_name(self):
        team = make_team()
        self.assertEqual(str(team), team.name)


# ---------------------------------------------------------------------------
# View tests – Player
# ---------------------------------------------------------------------------

class PlayerViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user()
        self.client.force_login(self.user)
        self.player = make_player(owner=self.user)

    def test_player_list_returns_200(self):
        response = self.client.get(reverse("player_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.player.name)

    def test_player_add_get_returns_200(self):
        response = self.client.get(reverse("player_add"))
        self.assertEqual(response.status_code, 200)

    def test_player_add_post_creates_player(self):
        data = {"name": "Charlie", "age": 18, "division": "B", "gender": "M"}
        response = self.client.post(reverse("player_add"), data)
        self.assertRedirects(response, reverse("player_list"))
        self.assertTrue(Player.objects.filter(name="Charlie").exists())

    def test_player_add_post_invalid_does_not_redirect(self):
        response = self.client.post(reverse("player_add"), {})
        self.assertEqual(response.status_code, 200)

    def test_player_edit_get_returns_200(self):
        response = self.client.get(reverse("player_edit", args=[self.player.pk]))
        self.assertEqual(response.status_code, 200)

    def test_player_edit_post_updates_player(self):
        data = {"name": "Alice Updated", "age": 21, "division": "A", "gender": "K"}
        response = self.client.post(reverse("player_edit", args=[self.player.pk]), data)
        self.assertRedirects(response, reverse("player_list"))
        self.player.refresh_from_db()
        self.assertEqual(self.player.name, "Alice Updated")

    def test_player_list_filters_by_division(self):
        make_player("A-spiller", division="A", owner=self.user)
        make_player("B-spiller", division="B", owner=self.user)
        response = self.client.get(reverse("player_list") + "?division=A")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A-spiller")

    def test_player_delete_get_shows_confirm(self):
        response = self.client.get(reverse("player_delete", args=[self.player.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.player.name)

    def test_player_delete_post_removes_player(self):
        response = self.client.post(reverse("player_delete", args=[self.player.pk]))
        self.assertRedirects(response, reverse("player_list"))
        self.assertFalse(Player.objects.filter(pk=self.player.pk).exists())

    def test_player_delete_nonexistent_returns_404(self):
        response = self.client.post(reverse("player_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# View tests – Team
# ---------------------------------------------------------------------------

class TeamViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='teamclub')
        self.client.force_login(self.user)
        p1 = make_player("Alice", owner=self.user)
        p2 = make_player("Bob", owner=self.user)
        self.team = Team.objects.create(player1=p1, player2=p2)

    def test_team_list_returns_200(self):
        response = self.client.get(reverse("team_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.team.name, html=True)

    def test_team_add_get_returns_200(self):
        response = self.client.get(reverse("team_add"))
        self.assertEqual(response.status_code, 200)

    def test_team_add_post_creates_team(self):
        p1 = make_player("Dave", gender='M', owner=self.user)
        p2 = make_player("Eve", gender='M', owner=self.user)  # same gender → double
        data = {"player1": p1.pk, "player2": p2.pk, "pair_type": "double"}
        response = self.client.post(reverse("team_add"), data)
        self.assertRedirects(response, reverse("team_list"))
        self.assertTrue(Team.objects.filter(player1=p1, player2=p2).exists())

    def test_team_add_double_wrong_gender_fails(self):
        p1 = make_player("Man1", gender='M', owner=self.user)
        p2 = make_player("Woman1", gender='K', owner=self.user)
        data = {"player1": p1.pk, "player2": p2.pk, "pair_type": "double"}
        response = self.client.post(reverse("team_add"), data)
        self.assertEqual(response.status_code, 200)  # form error, no redirect
        self.assertFalse(Team.objects.filter(player1=p1, player2=p2).exists())

    def test_team_add_mixed_wrong_gender_fails(self):
        p1 = make_player("Man2", gender='M', owner=self.user)
        p2 = make_player("Man3", gender='M', owner=self.user)
        data = {"player1": p1.pk, "player2": p2.pk, "pair_type": "mixed"}
        response = self.client.post(reverse("team_add"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Team.objects.filter(player1=p1, player2=p2).exists())

    def test_team_add_mixed_correct_genders_succeeds(self):
        p1 = make_player("Man4", gender='M', owner=self.user)
        p2 = make_player("Woman2", gender='K', owner=self.user)
        data = {"player1": p1.pk, "player2": p2.pk, "pair_type": "mixed"}
        response = self.client.post(reverse("team_add"), data)
        self.assertRedirects(response, reverse("team_list"))
        self.assertTrue(Team.objects.filter(player1=p1, player2=p2, pair_type='mixed').exists())

    def test_team_add_no_pair_type_with_two_players_fails(self):
        p1 = make_player("Man5", gender='M', owner=self.user)
        p2 = make_player("Man6", gender='M', owner=self.user)
        data = {"player1": p1.pk, "player2": p2.pk}
        response = self.client.post(reverse("team_add"), data)
        self.assertEqual(response.status_code, 200)

    def test_team_edit_get_returns_200(self):
        response = self.client.get(reverse("team_edit", args=[self.team.pk]))
        self.assertEqual(response.status_code, 200)

    def test_team_edit_nonexistent_returns_404(self):
        response = self.client.get(reverse("team_edit", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_team_delete_get_shows_confirm(self):
        response = self.client.get(reverse("team_delete", args=[self.team.pk]))
        self.assertEqual(response.status_code, 200)

    def test_team_delete_post_removes_team(self):
        pk = self.team.pk
        response = self.client.post(reverse("team_delete", args=[pk]))
        self.assertRedirects(response, reverse("team_list"))
        self.assertFalse(Team.objects.filter(pk=pk).exists())

    def test_team_delete_nonexistent_returns_404(self):
        response = self.client.get(reverse("team_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_team_list_search_by_name(self):
        response = self.client.get(reverse("team_list") + "?search=alice")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alice")

    def test_team_list_filter_by_pair_type(self):
        response = self.client.get(reverse("team_list") + "?pair_type=double")
        self.assertEqual(response.status_code, 200)

    def test_team_edit_post_saves_changes(self):
        from players.models import Player
        p1 = make_player("EditP1", gender="M", owner=self.user)
        p2 = make_player("EditP2", gender="M", owner=self.user)
        team = Team.objects.create(player1=p1, player2=p2, pair_type="double")
        data = {"player1": p1.pk, "player2": p2.pk, "pair_type": "double"}
        response = self.client.post(reverse("team_edit", args=[team.pk]), data)
        self.assertRedirects(response, reverse("team_list"))


# ---------------------------------------------------------------------------
# player_clear_rest view
# ---------------------------------------------------------------------------

class PlayerClearRestTest(TestCase):
    def setUp(self):
        import datetime as dt
        from django.utils import timezone
        self.client = Client()
        self.user = make_user(username='restclub')
        self.client.force_login(self.user)
        # Create a player with rest_until set
        self.player = make_player("Resting Player", owner=self.user)
        self.player.rest_until = timezone.now() + dt.timedelta(minutes=20)
        self.player.save()

    def test_clear_rest_removes_rest_until(self):
        response = self.client.post(reverse("player_clear_rest", args=[self.player.pk]))
        self.assertRedirects(response, reverse("player_list"))
        self.player.refresh_from_db()
        self.assertIsNone(self.player.rest_until)

    def test_clear_rest_get_does_not_clear(self):
        response = self.client.get(reverse("player_clear_rest", args=[self.player.pk]))
        self.assertRedirects(response, reverse("player_list"))
        self.player.refresh_from_db()
        self.assertIsNotNone(self.player.rest_until)


# ---------------------------------------------------------------------------
# player_toggle_arrived view
# ---------------------------------------------------------------------------

class PlayerToggleArrivedTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='arrivedclub')
        self.client.force_login(self.user)
        self.player = make_player("Not Arrived Yet", owner=self.user)

    def test_toggle_marks_player_arrived(self):
        self.assertFalse(self.player.has_arrived)
        response = self.client.post(reverse("player_toggle_arrived", args=[self.player.pk]))
        self.assertRedirects(response, reverse("player_list"))
        self.player.refresh_from_db()
        self.assertTrue(self.player.has_arrived)

    def test_toggle_again_marks_player_not_arrived(self):
        self.player.has_arrived = True
        self.player.save()
        self.client.post(reverse("player_toggle_arrived", args=[self.player.pk]))
        self.player.refresh_from_db()
        self.assertFalse(self.player.has_arrived)

    def test_toggle_redirects_to_next(self):
        response = self.client.post(
            reverse("player_toggle_arrived", args=[self.player.pk]), {'next': '/tournaments/1/run/'}
        )
        self.assertRedirects(response, '/tournaments/1/run/', fetch_redirect_response=False)

    def test_ajax_toggle_returns_json_without_redirect(self):
        response = self.client.post(
            reverse("player_toggle_arrived", args=[self.player.pk]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'has_arrived': True})
        self.player.refresh_from_db()
        self.assertTrue(self.player.has_arrived)

    def test_toggle_get_does_not_change(self):
        response = self.client.get(reverse("player_toggle_arrived", args=[self.player.pk]))
        self.assertRedirects(response, reverse("player_list"))
        self.player.refresh_from_db()
        self.assertFalse(self.player.has_arrived)

    def test_toggle_404_for_other_owner(self):
        other = make_user(username='otherarrivedclub')
        self.client.force_login(other)
        response = self.client.post(reverse("player_toggle_arrived", args=[self.player.pk]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# player_schedule_print view
# ---------------------------------------------------------------------------

class PlayerSchedulePrintTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='printplayer')
        self.client.force_login(self.user)
        self.player = make_player("PrintPlayer", owner=self.user)

    def test_player_schedule_print_returns_200(self):
        response = self.client.get(
            reverse("player_schedule_print", args=[self.player.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_player_schedule_print_404_for_other_owner(self):
        other = make_user("otheruser2")
        p = make_player("Other Player", owner=other)
        response = self.client.get(reverse("player_schedule_print", args=[p.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.status_code, 200)

    def test_team_delete_post_removes_team(self):
        response = self.client.post(reverse("team_delete", args=[self.team.pk]))
        self.assertRedirects(response, reverse("team_list"))
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())

    def test_team_delete_nonexistent_returns_404(self):
        response = self.client.post(reverse("team_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Player list – sorting + filtering
# ---------------------------------------------------------------------------

class PlayerListFilterSortTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='filterclub')
        self.client.force_login(self.user)
        self.alice = make_player("Alice", age=20, division="A", gender="K", owner=self.user)
        self.bob = make_player("Bob", age=18, division="B", gender="M", owner=self.user)
        self.charlie = make_player("Charlie", age=22, division="A", gender="M", owner=self.user)

    def test_filter_by_gender(self):
        response = self.client.get(reverse("player_list") + "?gender=M")
        self.assertContains(response, "Bob")
        self.assertNotContains(response, "Alice")

    def test_filter_by_search(self):
        response = self.client.get(reverse("player_list") + "?search=ali")
        self.assertContains(response, "Alice")
        self.assertNotContains(response, "Bob")

    def test_sort_by_name_desc(self):
        response = self.client.get(reverse("player_list") + "?sort=name&dir=desc")
        self.assertEqual(response.status_code, 200)
        names = [p.name for p in response.context['players']]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_sort_by_age(self):
        response = self.client.get(reverse("player_list") + "?sort=age")
        self.assertEqual(response.status_code, 200)
        ages = [p.age for p in response.context['players']]
        self.assertEqual(ages, sorted(ages))

    def test_sort_invalid_field_falls_back_to_name(self):
        response = self.client.get(reverse("player_list") + "?sort=invalid_field")
        self.assertEqual(response.status_code, 200)

    def test_filter_by_division_and_gender_combined(self):
        response = self.client.get(reverse("player_list") + "?division=A&gender=M")
        self.assertContains(response, "Charlie")
        self.assertNotContains(response, "Alice")
        self.assertNotContains(response, "Bob")


# ---------------------------------------------------------------------------
# Player schedule print
# ---------------------------------------------------------------------------

class PlayerSchedulePrintTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='printclub')
        self.client.force_login(self.user)
        self.player = make_player("PrintPlayer", owner=self.user)

    def test_schedule_print_returns_200_no_matches(self):
        response = self.client.get(reverse("player_schedule_print", args=[self.player.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PrintPlayer")

    def test_schedule_print_404_for_nonexistent(self):
        response = self.client.get(reverse("player_schedule_print", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_schedule_print_shows_matches(self):
        import datetime as dt
        from django.utils import timezone
        from tournaments.models import Tournament, Division, Match
        tournament = Tournament.objects.create(
            name="Print T", date=dt.date(2026, 6, 1),
            division_model='mixed', scoring_model='best_of_3_21',
        )
        division = Division.objects.create(
            tournament=tournament, name="D", discipline='single',
        )
        team = Team.objects.create(player1=self.player, player2=None, name=self.player.name)
        other = make_player("Opponent")
        team2 = Team.objects.create(player1=other, player2=None, name=other.name)
        Match.objects.create(
            division=division, team1=team, team2=team2,
            match_number=1,
            scheduled_time=timezone.make_aware(dt.datetime(2026, 6, 1, 10, 0)),
            court='1', status='pending',
        )
        response = self.client.get(reverse("player_schedule_print", args=[self.player.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Opponent")


# ---------------------------------------------------------------------------
# Team model – save edge cases
# ---------------------------------------------------------------------------

class TeamModelSaveTest(TestCase):
    def test_single_player_team_uses_player_name(self):
        p = make_player("Solo")
        team = Team.objects.create(player1=p, player2=None)
        self.assertEqual(team.name, "Solo")

    def test_auto_name_two_players_alphabetical(self):
        p1 = make_player("Zara")
        p2 = make_player("Anna")
        team = Team.objects.create(player1=p1, player2=p2)
        # Name should include both
        self.assertIn("Zara", team.name)
        self.assertIn("Anna", team.name)


# ---------------------------------------------------------------------------
# Helpers shared by new test classes
# ---------------------------------------------------------------------------

def _make_xlsx_bytes(rows, headers=('name', 'age', 'gender', 'division')):
    """Return an in-memory .xlsx file as bytes with the given headers and rows."""
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def _make_upload_file(content: bytes, filename='players.xlsx'):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(
        filename,
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


def _make_tournament_division(team):
    """Create a Tournament + Division and register *team* in it."""
    import datetime as dt
    from tournaments.models import Tournament, Division
    t = Tournament.objects.create(
        name='Test Cup', date=dt.date(2026, 7, 1),
        division_model='mixed', scoring_model='best_of_3_21',
    )
    div = Division.objects.create(tournament=t, name='A', discipline='single')
    div.teams.add(team)
    return div


# ---------------------------------------------------------------------------
# player_template_download
# ---------------------------------------------------------------------------

class PlayerTemplateDownloadTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='dlclub')
        self.client.force_login(self.user)

    def test_download_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('player_template_download'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_download_returns_xlsx(self):
        import os
        # Only run if the template file exists (it is copied during Docker build;
        # in local dev the file may not be present at players/excel/players.xlsx).
        from pathlib import Path
        path = Path(__file__).parent / 'excel' / 'players.xlsx'
        if not path.is_file():
            self.skipTest('players/excel/players.xlsx not present in this environment')
        response = self.client.get(reverse('player_template_download'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get('Content-Type'),
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )


# ---------------------------------------------------------------------------
# player_upload (Excel bulk import)
# ---------------------------------------------------------------------------

class PlayerUploadTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='uploadclub')
        self.client.force_login(self.user)
        # Create a valid division category so the upload can match it
        from players.models import DivisionCategory
        DivisionCategory.objects.create(name='A', owner=self.user)
        DivisionCategory.objects.create(name='B', owner=self.user)

    # ── GET redirect ───────────────────────────────────────────────────────

    def test_get_redirects_to_player_list(self):
        response = self.client.get(reverse('player_upload'))
        self.assertRedirects(response, reverse('player_list'))

    # ── missing / wrong file ───────────────────────────────────────────────

    def test_no_file_shows_error(self):
        response = self.client.post(reverse('player_upload'), {})
        self.assertRedirects(response, reverse('player_list'))
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('No file' in m or 'Ingen fil' in m for m in msgs))

    def test_wrong_extension_shows_error(self):
        f = _make_upload_file(b'not an xlsx', filename='players.csv')
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertRedirects(response, reverse('player_list'))
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('.xlsx' in m for m in msgs))

    def test_corrupt_file_shows_error(self):
        f = _make_upload_file(b'this is not a valid xlsx file at all')
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertRedirects(response, reverse('player_list'))
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('Could not read' in m or 'Kunne ikke' in m for m in msgs))

    # ── successful imports ─────────────────────────────────────────────────

    def test_valid_rows_create_players(self):
        data = _make_xlsx_bytes([('Alice', 20, 'K', 'A'), ('Bob', 18, 'M', 'B')])
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertRedirects(response, reverse('player_list'))
        self.assertTrue(Player.objects.filter(name='Alice', owner=self.user).exists())
        self.assertTrue(Player.objects.filter(name='Bob', owner=self.user).exists())

    def test_gender_mapping_lowercase(self):
        """m → M, k → K, f → K"""
        data = _make_xlsx_bytes([
            ('P1', 20, 'm', 'A'),
            ('P2', 20, 'k', 'A'),
            ('P3', 20, 'f', 'A'),
        ])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertEqual(Player.objects.get(name='P1', owner=self.user).gender, 'M')
        self.assertEqual(Player.objects.get(name='P2', owner=self.user).gender, 'K')
        self.assertEqual(Player.objects.get(name='P3', owner=self.user).gender, 'K')

    def test_unknown_gender_skips_row(self):
        data = _make_xlsx_bytes([('Ghost', 20, 'X', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertFalse(Player.objects.filter(name='Ghost', owner=self.user).exists())

    def test_unknown_division_adds_player_without_division(self):
        data = _make_xlsx_bytes([('NoDivPlayer', 20, 'M', 'UNKNOWN')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        p = Player.objects.filter(name='NoDivPlayer', owner=self.user).first()
        self.assertIsNotNone(p)
        self.assertEqual(p.division, '')

    def test_empty_name_row_is_skipped(self):
        data = _make_xlsx_bytes([('', 20, 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertEqual(Player.objects.filter(owner=self.user).count(), 0)

    def test_players_belong_to_logged_in_user(self):
        data = _make_xlsx_bytes([('MyPlayer', 20, 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        p = Player.objects.get(name='MyPlayer')
        self.assertEqual(p.owner, self.user)

    def test_success_message_contains_count(self):
        data = _make_xlsx_bytes([('Alice', 20, 'K', 'A'), ('Bob', 18, 'M', 'A')])
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('2' in m for m in msgs))

    # ── structural validation ──────────────────────────────────────────────

    def test_missing_column_aborts_entire_upload(self):
        """If a required column is missing no players are created."""
        data = _make_xlsx_bytes([('Alice', 20, 'U13')], headers=('name', 'age', 'division'))
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertRedirects(response, reverse('player_list'))
        self.assertEqual(Player.objects.filter(owner=self.user).count(), 0)
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('missing' in m.lower() or 'falten' in m.lower() for m in msgs))

    def test_missing_multiple_columns_names_them_all(self):
        data = _make_xlsx_bytes([(20,)], headers=('age',))
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        msgs = [str(m) for m in response.wsgi_request._messages]
        combined = ' '.join(msgs)
        self.assertIn('name', combined)
        self.assertIn('gender', combined)

    # ── row-level age validation ───────────────────────────────────────────

    def test_age_above_100_skips_row_with_error(self):
        data = _make_xlsx_bytes([('OldPlayer', 101, 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertFalse(Player.objects.filter(name='OldPlayer', owner=self.user).exists())

    def test_age_zero_skips_row_with_error(self):
        data = _make_xlsx_bytes([('ZeroAge', 0, 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertFalse(Player.objects.filter(name='ZeroAge', owner=self.user).exists())

    def test_age_negative_skips_row_with_error(self):
        data = _make_xlsx_bytes([('NegAge', -5, 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertFalse(Player.objects.filter(name='NegAge', owner=self.user).exists())

    def test_age_text_skips_row_with_error(self):
        data = _make_xlsx_bytes([('TextAge', 'twenty', 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertFalse(Player.objects.filter(name='TextAge', owner=self.user).exists())

    def test_blank_age_stores_as_none(self):
        data = _make_xlsx_bytes([('NoAge', '', 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        p = Player.objects.filter(name='NoAge', owner=self.user).first()
        self.assertIsNotNone(p)
        self.assertIsNone(p.age)

    def test_age_100_is_valid(self):
        data = _make_xlsx_bytes([('CentPlayer', 100, 'M', 'A')])
        f = _make_upload_file(data)
        self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertTrue(Player.objects.filter(name='CentPlayer', owner=self.user).exists())

    # ── row-level gender validation ────────────────────────────────────────

    def test_unknown_gender_produces_error_message(self):
        data = _make_xlsx_bytes([('Ghost', 20, 'X', 'A')])
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('Ghost' in m for m in msgs))

    # ── row-level name validation ──────────────────────────────────────────

    def test_empty_name_produces_error_message(self):
        data = _make_xlsx_bytes([('', 20, 'M', 'A')])
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('name' in m.lower() or 'navn' in m.lower() for m in msgs))

    # ── mixed valid and invalid rows ───────────────────────────────────────

    def test_valid_rows_created_despite_invalid_rows(self):
        data = _make_xlsx_bytes([
            ('Alice', 20, 'K', 'A'),    # valid
            ('BadAge', 999, 'M', 'A'),  # invalid age
            ('Bob', 18, 'M', 'A'),      # valid
        ])
        f = _make_upload_file(data)
        response = self.client.post(reverse('player_upload'), {'excel_file': f})
        self.assertTrue(Player.objects.filter(name='Alice', owner=self.user).exists())
        self.assertTrue(Player.objects.filter(name='Bob', owner=self.user).exists())
        self.assertFalse(Player.objects.filter(name='BadAge', owner=self.user).exists())
        msgs = [str(m) for m in response.wsgi_request._messages]
        # Warning because some succeeded and some failed
        self.assertTrue(any('2' in m for m in msgs))
        self.assertTrue(any('BadAge' in m for m in msgs))


# ---------------------------------------------------------------------------
# player_delete – tournament guard
# ---------------------------------------------------------------------------

class PlayerDeleteTournamentGuardTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='guardclub')
        self.client.force_login(self.user)

    def test_delete_free_player_succeeds(self):
        player = make_player('Free', owner=self.user)
        response = self.client.post(reverse('player_delete', args=[player.pk]))
        self.assertRedirects(response, reverse('player_list'))
        self.assertFalse(Player.objects.filter(pk=player.pk).exists())

    def test_delete_player_in_tournament_is_blocked(self):
        player = make_player('Blocked', owner=self.user)
        team = Team.objects.create(player1=player, player2=None, name=player.name)
        _make_tournament_division(team)
        response = self.client.post(reverse('player_delete', args=[player.pk]))
        self.assertRedirects(response, reverse('player_list'))
        # Player must still exist
        self.assertTrue(Player.objects.filter(pk=player.pk).exists())

    def test_delete_player_also_removes_player2_team(self):
        """When player is player2 in an unteamed (free) team, that team is deleted too."""
        p1 = make_player('P1', owner=self.user)
        p2 = make_player('P2', owner=self.user)
        team = Team.objects.create(player1=p1, player2=p2)
        # team is NOT in any tournament
        pk_team = team.pk
        self.client.post(reverse('player_delete', args=[p2.pk]))
        self.assertFalse(Player.objects.filter(pk=p2.pk).exists())
        self.assertFalse(Team.objects.filter(pk=pk_team).exists())


# ---------------------------------------------------------------------------
# player_bulk_delete
# ---------------------------------------------------------------------------

class PlayerBulkDeleteTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='bulkclub')
        self.client.force_login(self.user)
        self.p1 = make_player('Alpha', owner=self.user)
        self.p2 = make_player('Beta', owner=self.user)
        self.p3 = make_player('Gamma', owner=self.user)

    def test_get_redirects(self):
        response = self.client.get(reverse('player_bulk_delete'))
        self.assertRedirects(response, reverse('player_list'))

    def test_no_ids_shows_warning(self):
        response = self.client.post(reverse('player_bulk_delete'), {})
        self.assertRedirects(response, reverse('player_list'))
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('No players' in m or 'Ingen spillere' in m for m in msgs))

    def test_delete_selected_players(self):
        response = self.client.post(
            reverse('player_bulk_delete'),
            {'player_ids': [self.p1.pk, self.p2.pk]},
        )
        self.assertRedirects(response, reverse('player_list'))
        self.assertFalse(Player.objects.filter(pk=self.p1.pk).exists())
        self.assertFalse(Player.objects.filter(pk=self.p2.pk).exists())
        self.assertTrue(Player.objects.filter(pk=self.p3.pk).exists())

    def test_success_message_contains_count(self):
        response = self.client.post(
            reverse('player_bulk_delete'),
            {'player_ids': [self.p1.pk, self.p2.pk]},
        )
        msgs = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('2' in m for m in msgs))

    def test_blocked_players_not_deleted(self):
        team = Team.objects.create(player1=self.p1, player2=None, name=self.p1.name)
        _make_tournament_division(team)
        response = self.client.post(
            reverse('player_bulk_delete'),
            {'player_ids': [self.p1.pk]},
        )
        self.assertRedirects(response, reverse('player_list'))
        self.assertTrue(Player.objects.filter(pk=self.p1.pk).exists())

    def test_mixed_blocked_and_free_partial_delete(self):
        team = Team.objects.create(player1=self.p1, player2=None, name=self.p1.name)
        _make_tournament_division(team)
        response = self.client.post(
            reverse('player_bulk_delete'),
            {'player_ids': [self.p1.pk, self.p2.pk]},
        )
        self.assertRedirects(response, reverse('player_list'))
        self.assertTrue(Player.objects.filter(pk=self.p1.pk).exists())   # blocked
        self.assertFalse(Player.objects.filter(pk=self.p2.pk).exists())  # deleted

    def test_cannot_delete_other_users_players(self):
        other = make_user('otherclub2')
        other_player = make_player('OtherGuy', owner=other)
        self.client.post(
            reverse('player_bulk_delete'),
            {'player_ids': [other_player.pk]},
        )
        self.assertTrue(Player.objects.filter(pk=other_player.pk).exists())

    def test_unauthenticated_redirects_to_login(self):
        self.client.logout()
        response = self.client.post(
            reverse('player_bulk_delete'),
            {'player_ids': [self.p1.pk]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

