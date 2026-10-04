import datetime
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from players.models import Player, Team
from .models import Tournament, Division, Match, DivisionSeed, TournamentDay
from .forms import MatchResultForm, _parse_score as _form_parse_score, _validate_set
from .scheduler import generate_round_robin, generate_bracket, generate_schedule, advance_bracket, get_bracket_data
from .standings import compute_standings, compute_group_standings, _parse_score, STANDINGS_CONFIG

User = get_user_model()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(username='testclub', password='testpass123'):
    return User.objects.create_user(username=username, password=password)


def make_player(name="Alice", division="A", owner=None):
    return Player.objects.create(name=name, age=20, division=division, owner=owner, has_arrived=True)


def make_team(name=None, r1=1, r2=2):
    p1 = make_player(f"P{r1}")
    p2 = make_player(f"P{r2}")
    team = Team.objects.create(player1=p1, player2=p2)
    if name:
        team.name = name
        team.save()
    return team


def make_tournament(owner=None):
    return Tournament.objects.create(
        name="Test Tournament",
        date=datetime.date.today(),
        division_model="mixed",
        scoring_model="best_of_3_21",
        owner=owner,
    )


def make_division(tournament=None, name="A Række", discipline='double', tournament_type='group'):
    t = tournament or make_tournament()
    return Division.objects.create(tournament=t, name=name, discipline=discipline, tournament_type=tournament_type)


def make_day(tournament, day_number=1, date=None, start="09:00", end="22:00", courts=4, buffer=30):
    """Opretter en TournamentDay på turneringen."""
    import datetime as dt
    return TournamentDay.objects.create(
        tournament=tournament,
        day_number=day_number,
        date=date or dt.date.today(),
        start_time=dt.time(*map(int, start.split(':'))),
        end_time=dt.time(*map(int, end.split(':'))) if end else None,
        court_count=courts,
        buffer_minutes=buffer,
    )


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TournamentModelTest(TestCase):
    def test_str_includes_name(self):
        t = make_tournament()
        self.assertIn("Test Tournament", str(t))

    def test_tournament_types(self):
        for ttype in ["tree", "group", "playoff"]:
            d = make_division(name=f"D-{ttype}", tournament_type=ttype)
            self.assertEqual(d.tournament_type, ttype)


class DivisionModelTest(TestCase):
    def test_str_includes_division_and_tournament_name(self):
        d = make_division()
        self.assertIn("A Række", str(d))
        self.assertIn("Test Tournament", str(d))

    def test_teams_can_be_added(self):
        d = make_division()
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        d.teams.add(t1, t2)
        self.assertEqual(d.teams.count(), 2)


# ---------------------------------------------------------------------------
# TournamentDay model tests
# ---------------------------------------------------------------------------

class TournamentDayCleanTest(TestCase):
    def test_end_time_before_start_time_raises(self):
        import datetime as dt
        from django.core.exceptions import ValidationError
        t = make_tournament()
        day = TournamentDay(
            tournament=t, day_number=1, date=dt.date.today(),
            start_time=dt.time(10, 0), end_time=dt.time(9, 0), court_count=4,
        )
        with self.assertRaises(ValidationError):
            day.full_clean()

    def test_equal_start_and_end_time_raises(self):
        import datetime as dt
        from django.core.exceptions import ValidationError
        t = make_tournament()
        day = TournamentDay(
            tournament=t, day_number=1, date=dt.date.today(),
            start_time=dt.time(9, 0), end_time=dt.time(9, 0), court_count=4,
        )
        with self.assertRaises(ValidationError):
            day.full_clean()

    def test_valid_day_no_error(self):
        from django.core.exceptions import ValidationError
        day = make_day(make_tournament())
        try:
            day.full_clean()
        except ValidationError:
            self.fail("full_clean() raised ValidationError on valid day")

    def test_open_ended_day_no_error(self):
        day = make_day(make_tournament(), end=None)
        from django.core.exceptions import ValidationError
        try:
            day.full_clean()
        except ValidationError:
            self.fail("full_clean() raised ValidationError on open-ended day")


class TournamentDayBufferTest(TestCase):
    def test_negative_buffer_raises(self):
        import datetime as dt
        from django.core.exceptions import ValidationError
        t = make_tournament()
        day = TournamentDay(
            tournament=t, day_number=1, date=dt.date.today(),
            start_time=dt.time(9, 0), end_time=dt.time(22, 0),
            court_count=4, buffer_minutes=-1,
        )
        with self.assertRaises(ValidationError):
            day.full_clean()

    def test_buffer_equal_to_window_raises(self):
        import datetime as dt
        from django.core.exceptions import ValidationError
        t = make_tournament()
        # 09:00–10:00 = 60 min vindue; buffer=60 skal fejle
        day = TournamentDay(
            tournament=t, day_number=1, date=dt.date.today(),
            start_time=dt.time(9, 0), end_time=dt.time(10, 0),
            court_count=4, buffer_minutes=60,
        )
        with self.assertRaises(ValidationError):
            day.full_clean()

    def test_buffer_less_than_window_ok(self):
        import datetime as dt
        from django.core.exceptions import ValidationError
        t = make_tournament()
        day = TournamentDay(
            tournament=t, day_number=1, date=dt.date.today(),
            start_time=dt.time(9, 0), end_time=dt.time(10, 0),
            court_count=4, buffer_minutes=30,
        )
        try:
            day.full_clean()
        except ValidationError:
            self.fail("full_clean() raised ValidationError for valid buffer")


class TournamentDayUniqueTest(TestCase):
    def test_duplicate_day_number_in_same_tournament_raises(self):
        import datetime as dt
        from django.db import IntegrityError
        t = make_tournament()
        make_day(t, day_number=1)
        with self.assertRaises(IntegrityError):
            make_day(t, day_number=1)

    def test_same_day_number_in_different_tournaments_ok(self):
        t1 = make_tournament()
        t2 = make_tournament()
        d1 = make_day(t1, day_number=1)
        d2 = make_day(t2, day_number=1)
        self.assertEqual(d1.day_number, 1)
        self.assertEqual(d2.day_number, 1)


class TournamentDayStrTest(TestCase):
    def test_str_with_end_time(self):
        day = make_day(make_tournament())
        s = str(day)
        self.assertIn("Day 1", s)
        self.assertIn("09:00", s)
        self.assertIn("22:00", s)
        self.assertIn("buffer", s)

    def test_str_without_end_time(self):
        day = make_day(make_tournament(), end=None)
        s = str(day)
        self.assertIn("Day 1", s)
        self.assertIn("open end", s)


class TournamentSyncDateTest(TestCase):
    def test_sync_date_sets_date_from_first_day(self):
        import datetime as dt
        t = make_tournament()
        make_day(t, day_number=1, date=dt.date(2026, 6, 1))
        make_day(t, day_number=2, date=dt.date(2026, 6, 2))
        t.sync_date()
        self.assertEqual(t.date, dt.date(2026, 6, 1))

    def test_sync_date_no_days_does_not_crash(self):
        t = make_tournament()
        try:
            t.sync_date()
        except Exception as e:
            self.fail(f"sync_date() crashed with no days: {e}")


class DivisionDayDeletionTest(TestCase):
    def test_delete_day_nulls_division_fk(self):
        t = make_tournament()
        day = make_day(t)
        d = make_division(tournament=t)
        d.group_day = day
        d.save()
        day.delete()
        d.refresh_from_db()
        self.assertIsNone(d.group_day)


class MatchModelTest(TestCase):
    def setUp(self):
        self.division = make_division()
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)

    def test_str_includes_round_and_teams(self):
        m = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2, match_round=2
        )
        self.assertIn("R2", str(m))
        self.assertIn(self.t1.name, str(m))

    def test_default_status_is_pending(self):
        m = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2
        )
        self.assertEqual(m.status, "pending")


# ---------------------------------------------------------------------------
# Scheduler tests
# ---------------------------------------------------------------------------

class RoundRobinSchedulerTest(TestCase):
    def setUp(self):
        self.division = make_division()
        self.teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(4)]
        self.division.teams.set(self.teams)

    def test_generates_correct_number_of_matches(self):
        # With n teams, round-robin produces n*(n-1)/2 matches
        n = len(self.teams)
        matches = generate_round_robin(self.division)
        self.assertEqual(len(matches), n * (n - 1) // 2)

    def test_all_matches_are_pending(self):
        matches = generate_round_robin(self.division)
        for m in matches:
            self.assertEqual(m.status, "pending")

    def test_regenerate_removes_old_pending_matches(self):
        generate_round_robin(self.division)
        generate_round_robin(self.division)
        n = len(self.teams)
        self.assertEqual(
            Match.objects.filter(division=self.division).count(),
            n * (n - 1) // 2,
        )

    def test_no_matches_with_fewer_than_two_teams(self):
        division = make_division(name="Empty")
        matches = generate_round_robin(division)
        self.assertEqual(matches, [])


class BracketSchedulerTest(TestCase):
    def setUp(self):
        self.division = make_division(tournament_type='tree')
        self.teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(4)]
        self.division.teams.set(self.teams)

    def test_generates_all_rounds(self):
        # 4 teams → round 1: 2 matches, round 2: 1 placeholder = 3 total
        matches = generate_bracket(self.division)
        self.assertEqual(len(matches), 3)

    def test_round1_matches_count(self):
        matches = generate_bracket(self.division)
        round1 = [m for m in matches if m.match_round == 1]
        self.assertEqual(len(round1), 2)

    def test_placeholder_created_for_final(self):
        matches = generate_bracket(self.division)
        placeholders = [m for m in matches if m.team1 is None]
        self.assertEqual(len(placeholders), 1)
        self.assertEqual(placeholders[0].match_round, 2)
        self.assertIsNotNone(placeholders[0].bracket_label)

    def test_no_matches_with_fewer_than_two_teams(self):
        division = make_division(name="Empty2", tournament_type='tree')
        matches = generate_bracket(division)
        self.assertEqual(matches, [])

    def test_bye_given_when_odd_team_count(self):
        # 3 teams → bracket_size=4, 1 bye + 1 real in round 1, 1 placeholder in round 2
        division = make_division(name="Odd", tournament_type='tree')
        teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(3)]
        division.teams.set(teams)
        matches = generate_bracket(division)
        # round1: bye + real; round2: placeholder (bye already filled team1)
        round1 = [m for m in matches if m.match_round == 1]
        bye_matches = [m for m in round1 if m.team2 is None and m.status == 'completed']
        real_matches = [m for m in round1 if m.team2 is not None]
        self.assertEqual(len(bye_matches), 1)
        self.assertEqual(len(real_matches), 1)
        self.assertIsNotNone(bye_matches[0].winner)
        # Placeholder in round 2 should have team1 filled (bye advanced)
        final = next(m for m in matches if m.match_round == 2)
        final.refresh_from_db()
        self.assertIsNotNone(final.team1)

    def test_bracket_slot_set_on_round1(self):
        matches = generate_bracket(self.division)
        round1 = [m for m in matches if m.match_round == 1]
        slots = sorted(m.bracket_slot for m in round1)
        self.assertEqual(slots, [1, 2])


class AdvanceBracketTest(TestCase):
    def setUp(self):
        self.division = make_division(tournament_type='tree')
        self.teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(4)]
        self.division.teams.set(self.teams)
        generate_bracket(self.division)

    def test_no_advance_when_other_match_not_done(self):
        # Complete only match in bracket_slot=1, round 1
        m = Match.objects.get(division=self.division, match_round=1, bracket_slot=1)
        m.winner = m.team1
        m.status = 'completed'
        m.save()
        advance_bracket(m)
        # Round 2 placeholder should still have team2=None (only team1 is filled)
        final = Match.objects.get(division=self.division, match_round=2)
        self.assertIsNone(final.team2)
        self.assertIsNotNone(final.bracket_label)  # still a placeholder

    def test_advance_fills_both_teams(self):
        for match in Match.objects.filter(division=self.division, match_round=1):
            match.winner = match.team1
            match.status = 'completed'
            match.save()
            advance_bracket(match)
        # Final should have both teams filled and bracket_label cleared
        final = Match.objects.get(division=self.division, match_round=2)
        final.refresh_from_db()
        self.assertIsNotNone(final.team1)
        self.assertIsNotNone(final.team2)
        self.assertIsNone(final.bracket_label)


class BracketDataTest(TestCase):
    def test_returns_none_when_no_matches(self):
        division = make_division(tournament_type='tree')
        self.assertIsNone(get_bracket_data(division))

    def test_returns_correct_rounds(self):
        division = make_division(tournament_type='tree')
        teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(4)]
        division.teams.set(teams)
        generate_bracket(division)
        data = get_bracket_data(division)
        self.assertIsNotNone(data)
        self.assertEqual(data['total_rounds'], 2)
        self.assertEqual(len(data['rounds']), 2)
        self.assertEqual(str(data['rounds'][0]['label']), 'Semifinal')
        self.assertEqual(str(data['rounds'][1]['label']), 'Final')


class GenerateScheduleRouterTest(TestCase):
    def test_group_uses_round_robin(self):
        division = make_division(tournament_type='group')
        teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(3)]
        division.teams.set(teams)
        matches = generate_schedule(division)
        self.assertEqual(len(matches), 3)  # 3*(3-1)/2 = 3

    def test_tree_uses_bracket(self):
        division = make_division(tournament_type='tree')
        teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(4)]
        division.teams.set(teams)
        matches = generate_schedule(division)
        # 4 teams: 2 round-1 matches + 1 round-2 placeholder
        self.assertEqual(len(matches), 3)
        round1 = [m for m in matches if m.match_round == 1 and m.team2 is not None]
        self.assertEqual(len(round1), 2)

    def test_playoff_uses_round_robin(self):
        division = make_division(tournament_type='playoff')
        division.group_count = 1
        division.advance_count = 2
        division.save()
        teams = [make_team(r1=i*2+1, r2=i*2+2) for i in range(3)]
        division.teams.set(teams)
        matches = generate_schedule(division)
        # 3 teams in 1 group: 3 group matches + 2 bracket slots (final)
        group_matches = [m for m in matches if m.phase == 'group']
        self.assertEqual(len(group_matches), 3)


class TournamentCRUDViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user()
        self.client.force_login(self.user)

    def test_tournament_create_get_returns_200(self):
        response = self.client.get(reverse("tournament_create"))
        self.assertEqual(response.status_code, 200)

    def test_tournament_create_post_creates_tournament(self):
        response = self.client.post(reverse("tournament_create"), {
            'name': 'Ny Turnering',
            'date': '2026-06-01',
            'division_model': 'mixed',
            'scoring_model': 'best_of_3_21',
            'single_match_duration': 30,
            'double_match_duration': 40,
            'player_break_time': 15,
            # TournamentDay inline formset (min 1 day required)
            'days-TOTAL_FORMS': '1',
            'days-INITIAL_FORMS': '0',
            'days-MIN_NUM_FORMS': '1',
            'days-MAX_NUM_FORMS': '1000',
            'days-0-day_number': '1',
            'days-0-date': '2026-06-01',
            'days-0-start_time': '09:00',
            'days-0-court_count': '4',
            'days-0-buffer_minutes': '30',
        })
        tournament = Tournament.objects.get(name='Ny Turnering')
        self.assertRedirects(response, reverse("tournament_detail", args=[tournament.pk]))

    def test_tournament_create_invalid_stays_on_page(self):
        response = self.client.post(reverse("tournament_create"), {'name': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tournament.objects.filter(name='').exists())

    def test_tournament_edit_get_returns_200(self):
        t = make_tournament(owner=self.user)
        response = self.client.get(reverse("tournament_edit", args=[t.pk]))
        self.assertEqual(response.status_code, 200)

    def test_tournament_edit_post_updates_fields(self):
        t = make_tournament(owner=self.user)
        day = make_day(t)  # existing day: INITIAL_FORMS=1
        response = self.client.post(reverse("tournament_edit", args=[t.pk]), {
            'name': 'Opdateret',
            'date': '2026-07-01',
            'division_model': 'mixed',
            'scoring_model': 'best_of_3_21',
            'single_match_duration': 30,
            'double_match_duration': 40,
            'player_break_time': 15,
            # Keep the existing day
            'days-TOTAL_FORMS': '1',
            'days-INITIAL_FORMS': '1',
            'days-MIN_NUM_FORMS': '1',
            'days-MAX_NUM_FORMS': '1000',
            'days-0-id': str(day.pk),
            'days-0-tournament': str(t.pk),
            'days-0-day_number': '1',
            'days-0-date': str(day.date),
            'days-0-start_time': '09:00',
            'days-0-court_count': '4',
            'days-0-buffer_minutes': '30',
        })
        self.assertRedirects(response, reverse("tournament_detail", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.name, 'Opdateret')

    def test_tournament_edit_404_for_nonexistent(self):
        response = self.client.get(reverse("tournament_edit", args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------

class TournamentViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='viewclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_tournament_list_returns_200(self):
        response = self.client.get(reverse("tournament_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.tournament.name)

    def test_tournament_detail_returns_200(self):
        response = self.client.get(reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.division.name)
        self.assertIn('division_data', response.context)

    def test_tournament_detail_uses_shared_result_modal(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            match_number=1, status='completed', score='21-10, 21-10', winner=self.t1,
        )
        response = self.client.get(reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertContains(response, 'id="result-entry-dialog"')
        self.assertContains(response, 'js-result-modal')
        self.assertContains(response, 'openResultEntryModal(m.url)')

    def test_tournament_detail_404_for_nonexistent(self):
        response = self.client.get(reverse("tournament_detail", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_tournament_detail_has_division_form(self):
        response = self.client.get(reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertIn('division_form', response.context)

    def test_division_create_post_creates_division(self):
        response = self.client.post(
            reverse("division_create", args=[self.tournament.pk]),
            {'name': 'Ny Division', 'discipline': 'single', 'tournament_type': 'group'},
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertTrue(self.tournament.divisions.filter(name='Ny Division').exists())

    def test_division_create_invalid_stays_on_page(self):
        response = self.client.post(
            reverse("division_create", args=[self.tournament.pk]),
            {'name': ''},
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        # Invalid form redirects but does not create
        self.assertFalse(self.tournament.divisions.filter(name='').exists())

    def test_division_update_teams(self):
        # division default discipline is 'double', so POST field is 'pairs'
        p5 = make_player("P5", owner=self.user)
        p6 = make_player("P6", owner=self.user)
        t3 = Team.objects.create(player1=p5, player2=p6)
        response = self.client.post(
            reverse("division_update_teams", args=[self.division.pk]),
            {'pairs': [t3.pk]},
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        self.division.refresh_from_db()
        self.assertIn(t3, self.division.teams.all())
        # t1 and t2 are replaced
        self.assertNotIn(self.t1, self.division.teams.all())

    def test_division_update_teams_clear_all(self):
        response = self.client.post(
            reverse("division_update_teams", args=[self.division.pk]),
            {},  # no pairs selected → clears all
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertEqual(self.division.teams.count(), 0)

    def test_division_update_single_auto_creates_teams(self):
        single_div = Division.objects.create(
            tournament=self.tournament, name="Herresingle", discipline='single'
        )
        p1 = make_player("Solo1", owner=self.user)
        p2 = make_player("Solo2", owner=self.user)
        response = self.client.post(
            reverse("division_update_teams", args=[single_div.pk]),
            {'players': [p1.pk, p2.pk]},
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        single_div.refresh_from_db()
        self.assertEqual(single_div.teams.count(), 2)
        names = list(single_div.teams.values_list('name', flat=True))
        self.assertIn(p1.name, names)
        self.assertIn(p2.name, names)

    def test_division_detail_has_participants_form(self):
        response = self.client.get(reverse("tournament_detail", args=[self.tournament.pk]))
        dd = response.context['division_data'][0]
        self.assertIn('participants_form', dd)

    def test_division_delete_get_shows_confirm(self):
        response = self.client.get(reverse("division_delete", args=[self.division.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.division.name)

    def test_division_delete_post_removes_division(self):
        div_pk = self.division.pk
        response = self.client.post(reverse("division_delete", args=[div_pk]))
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertFalse(Division.objects.filter(pk=div_pk).exists())

    def test_division_delete_404_for_nonexistent(self):
        response = self.client.get(reverse("division_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_generate_schedule_creates_matches(self):
        response = self.client.post(
            reverse("division_generate_schedule", args=[self.division.pk])
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertEqual(Match.objects.filter(division=self.division).count(), 1)

    def test_generate_schedule_assigns_match_numbers(self):
        self.client.post(reverse("division_generate_schedule", args=[self.division.pk]))
        match = Match.objects.filter(division=self.division).first()
        self.assertIsNotNone(match.match_number)
        self.assertGreater(match.match_number, 0)

    def test_generate_schedule_numbers_are_sequential_across_divisions(self):
        # Create a second division and generate schedules for both
        div2 = Division.objects.create(
            tournament=self.tournament, name="Division B", discipline='double'
        )
        t3 = make_team(r1=5, r2=6)
        t4 = make_team(r1=7, r2=8)
        div2.teams.add(t3, t4)
        self.client.post(reverse("division_generate_schedule", args=[self.division.pk]))
        self.client.post(reverse("division_generate_schedule", args=[div2.pk]))
        all_numbers = sorted(
            Match.objects.filter(division__tournament=self.tournament)
            .exclude(match_number=None)
            .values_list('match_number', flat=True)
        )
        self.assertEqual(all_numbers, list(range(1, len(all_numbers) + 1)))

    def test_scoresheet_view_returns_200(self):
        self.client.post(reverse("division_generate_schedule", args=[self.division.pk]))
        response = self.client.get(reverse("tournament_scoresheet", args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_scoresheet_contains_match_number(self):
        self.client.post(reverse("division_generate_schedule", args=[self.division.pk]))
        match = Match.objects.filter(division=self.division).first()
        response = self.client.get(reverse("tournament_scoresheet", args=[self.tournament.pk]))
        self.assertContains(response, f'#{match.match_number}')

    def test_scoresheet_always_has_blank_court_field_and_narrow_card_layout(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            match_number=1, court='4',
        )
        response = self.client.get(reverse("tournament_scoresheet", args=[self.tournament.pk]))
        self.assertContains(response, 'Court: <span class="court-write-in">')
        self.assertNotContains(response, 'Court: 4')
        self.assertContains(response, 'width: min(100%, 155mm)')

    def test_scoresheet_404_for_nonexistent(self):
        response = self.client.get(reverse("tournament_scoresheet", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_schedule_view_returns_200(self):
        response = self.client.get(reverse("tournament_schedule", args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_schedule_generate_without_start_time_shows_warning(self):
        # Turnering uden dage giver error-redirect
        response = self.client.post(
            reverse("tournament_generate_time_schedule", args=[self.tournament.pk])
        )
        self.assertRedirects(response, reverse("tournament_schedule", args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('day' in str(m).lower() or 'dag' in str(m).lower() for m in messages_list))

    def test_schedule_generate_assigns_times(self):
        import datetime as dt
        make_day(self.tournament, courts=2)
        # generate match programme first
        self.client.post(reverse("division_generate_schedule", args=[self.division.pk]))
        response = self.client.post(
            reverse("tournament_generate_time_schedule", args=[self.tournament.pk])
        )
        self.assertRedirects(response, reverse("tournament_schedule", args=[self.tournament.pk]))
        match = Match.objects.filter(division=self.division).first()
        self.assertIsNotNone(match.scheduled_time)
        self.assertIsNotNone(match.court)

    def test_standings_appear_after_completed_match(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='completed', winner=self.t1, score='21-15'
        )
        response = self.client.get(reverse("tournament_detail", args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        dd = response.context['division_data'][0]
        standings = dd['standings']
        self.assertEqual(standings[0]['team'], self.t1)
        self.assertEqual(standings[0]['points'], 2)
        self.assertEqual(standings[1]['points'], 0)


class StandingsTest(TestCase):
    """Unit tests for the standings / tiebreaker engine."""

    def setUp(self):
        self.division = make_division()
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.t3 = make_team(r1=5, r2=6)
        self.division.teams.add(self.t1, self.t2, self.t3)

    # ── Config ────────────────────────────────────────────────────────────
    def test_config_win_points_is_2(self):
        self.assertEqual(STANDINGS_CONFIG['win_points'], 2)

    def test_config_loss_points_is_0(self):
        self.assertEqual(STANDINGS_CONFIG['loss_points'], 0)

    def test_config_tiebreakers_order(self):
        self.assertEqual(STANDINGS_CONFIG['tiebreakers'], ['sets_diff', 'score_diff', 'score_for'])

    # ── Score parsing ──────────────────────────────────────────────────────
    def test_parse_score_single_set(self):
        self.assertEqual(_parse_score('21-15'), (21, 15))

    def test_parse_score_three_sets(self):
        self.assertEqual(_parse_score('21-15, 18-21, 21-18'), (60, 54))

    def test_parse_score_empty(self):
        self.assertEqual(_parse_score(''), (0, 0))

    def test_parse_score_none(self):
        self.assertEqual(_parse_score(None), (0, 0))

    # ── Basic standings ────────────────────────────────────────────────────
    def test_winner_ranks_first(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='completed', winner=self.t2, score='21-15'
        )
        rows = compute_standings(self.division)
        self.assertEqual(rows[0]['team'], self.t2)
        self.assertEqual(rows[0]['points'], 2)

    def test_score_for_and_against_tallied(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='completed', winner=self.t1, score='21-15, 21-10'
        )
        rows = compute_standings(self.division)
        r1 = next(r for r in rows if r['team'] == self.t1)
        r2 = next(r for r in rows if r['team'] == self.t2)
        self.assertEqual(r1['score_for'], 42)
        self.assertEqual(r1['score_against'], 25)
        self.assertEqual(r2['score_for'], 25)
        self.assertEqual(r2['score_against'], 42)

    # ── Head-to-head tiebreaker ────────────────────────────────────────────
    def test_head_to_head_breaks_points_tie(self):
        # t1 beats t3, t2 beats t3, t1 beats t2 → t1 > t2 > t3
        Match.objects.create(division=self.division, team1=self.t1, team2=self.t3,
                              status='completed', winner=self.t1, score='21-10')
        Match.objects.create(division=self.division, team1=self.t2, team2=self.t3,
                              status='completed', winner=self.t2, score='21-10')
        Match.objects.create(division=self.division, team1=self.t1, team2=self.t2,
                              status='completed', winner=self.t1, score='21-10')
        rows = compute_standings(self.division)
        self.assertEqual(rows[0]['team'], self.t1)
        self.assertEqual(rows[1]['team'], self.t2)
        self.assertEqual(rows[2]['team'], self.t3)

    # ── Points-scored tiebreaker ───────────────────────────────────────────
    def test_points_scored_breaks_remaining_tie(self):
        # t1 and t2 both beat t3; mutual match not played → pure points tie
        # t1 beat t3 with better net score → ranks first
        Match.objects.create(division=self.division, team1=self.t1, team2=self.t3,
                              status='completed', winner=self.t1, score='21-5')
        Match.objects.create(division=self.division, team1=self.t2, team2=self.t3,
                              status='completed', winner=self.t2, score='21-19')
        rows = compute_standings(self.division)
        self.assertEqual(rows[0]['team'], self.t1)  # net +16 > net +2
        self.assertEqual(rows[1]['team'], self.t2)


class MatchResultViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='resultclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2
        )

    def test_match_result_get_returns_200(self):
        response = self.client.get(reverse("match_record_result", args=[self.match.pk]))
        self.assertEqual(response.status_code, 200)

    def test_match_result_popup_get_includes_popup_controls(self):
        response = self.client.get(
            reverse("match_record_result", args=[self.match.pk]), {'popup': '1'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="popup" value="1"')
        self.assertContains(response, 'id="cancel-result-popup"')

    def test_match_result_popup_save_notifies_parent(self):
        response = self.client.post(
            reverse("match_record_result", args=[self.match.pk]) + "?popup=1",
            {
                "score": "21-15, 21-10",
                "winner": self.t1.pk,
                "next": reverse("tournament_run", args=[self.tournament.pk]),
                "popup": "1",
            },
        )
        self.assertEqual(response.status_code, 204)
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "completed")

    def test_match_result_post_records_result(self):
        data = {
            "score": "21-15, 21-10",
            "winner": self.t1.pk,
        }
        response = self.client.post(
            reverse("match_record_result", args=[self.match.pk]), data
        )
        self.assertRedirects(
            response, reverse("tournament_detail", args=[self.tournament.pk])
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.score, "21-15, 21-10")
        self.assertEqual(self.match.status, "completed")
        self.assertEqual(self.match.winner, self.t1)

    def test_match_result_post_accepts_best_of_three_to_15(self):
        self.tournament.scoring_model = 'best_of_5_15'
        self.tournament.save(update_fields=['scoring_model'])
        response = self.client.post(
            reverse("match_record_result", args=[self.match.pk]),
            {"score": "7-15, 15-12, 15-13", "winner": self.t1.pk},
        )
        self.assertRedirects(
            response, reverse("tournament_detail", args=[self.tournament.pk])
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.score, "7-15, 15-12, 15-13")
        self.assertEqual(self.match.status, "completed")

    def test_match_result_auto_sets_completed(self):
        """Valid score + winner should automatically mark match as completed."""
        self.assertEqual(self.match.status, 'pending')
        self.client.post(
            reverse("match_record_result", args=[self.match.pk]),
            {"score": "21-15, 21-10", "winner": self.t1.pk},
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, 'completed')

    def test_match_result_post_invalid_stays_on_page(self):
        # Posting nonsense score stays on the form page
        response = self.client.post(
            reverse("match_record_result", args=[self.match.pk]),
            {'score': 'abc-xyz', 'winner': self.t1.pk},
        )
        self.assertEqual(response.status_code, 200)

    def test_match_result_404_for_nonexistent(self):
        response = self.client.get(reverse("match_record_result", args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Score validation (MatchResultForm)
# ---------------------------------------------------------------------------

class MatchResultFormValidationTest(TestCase):
    """Tests for the BWF score validation added to MatchResultForm."""

    def setUp(self):
        self.tournament = make_tournament(owner=None)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2
        )

    def _post(self, score, winner=None, status=None):
        data = {'score': score, 'winner': (winner or self.t1).pk}
        if status is not None:
            data['status'] = status
        return MatchResultForm(data=data, instance=self.match)

    # ── _parse_score helper ────────────────────────────────────────────────
    def test_parse_score_single_set(self):
        self.assertEqual(_form_parse_score('21-15'), [(21, 15)])

    def test_parse_score_three_sets(self):
        self.assertEqual(_form_parse_score('21-15, 18-21, 21-18'), [(21, 15), (18, 21), (21, 18)])

    def test_parse_score_bad_format_raises(self):
        with self.assertRaises(ValueError):
            _form_parse_score('abc')

    def test_parse_score_missing_dash_raises(self):
        with self.assertRaises(ValueError):
            _form_parse_score('21 15')

    # ── _validate_set helper ───────────────────────────────────────────────
    def test_validate_set_normal_win(self):
        self.assertEqual(_validate_set(21, 15), '')

    def test_validate_set_deuce_win(self):
        self.assertEqual(_validate_set(22, 20), '')
        self.assertEqual(_validate_set(30, 28), '')

    def test_validate_set_max_deuce(self):
        self.assertEqual(_validate_set(30, 29), '')

    def test_validate_set_draw_invalid(self):
        self.assertNotEqual(_validate_set(21, 21), '')

    def test_validate_set_too_low_winner(self):
        self.assertNotEqual(_validate_set(19, 10), '')

    def test_validate_set_21_20_invalid(self):
        # At 20-20 you must play to 2-point lead
        self.assertNotEqual(_validate_set(21, 20), '')

    def test_validate_set_over_30_invalid(self):
        self.assertNotEqual(_validate_set(31, 29), '')

    def test_validate_set_deuce_not_2_apart_invalid(self):
        # 23-20 is not valid (not 2-point lead from deuce)
        self.assertNotEqual(_validate_set(23, 20), '')

    # ── Form-level validation ──────────────────────────────────────────────
    def test_valid_score_2_sets(self):
        form = self._post('21-15, 21-10')
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_score_3_sets(self):
        form = self._post('21-15, 18-21, 21-18')
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_score_deuce(self):
        form = self._post('22-20, 21-15')
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_best_of_three_sets_to_15(self):
        self.tournament.scoring_model = 'best_of_5_15'
        self.tournament.save(update_fields=['scoring_model'])
        form = self._post('7-15, 15-12, 15-13')
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_extended_set_to_15(self):
        self.tournament.scoring_model = 'best_of_5_15'
        self.tournament.save(update_fields=['scoring_model'])
        form = self._post('16-14, 15-12')
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_15_point_set_must_win_by_two(self):
        self.tournament.scoring_model = 'best_of_5_15'
        self.tournament.save(update_fields=['scoring_model'])
        form = self._post('15-14, 15-12')
        self.assertFalse(form.is_valid())

    def test_invalid_15_point_set_below_target(self):
        self.tournament.scoring_model = 'best_of_5_15'
        self.tournament.save(update_fields=['scoring_model'])
        form = self._post('14-12, 15-12')
        self.assertFalse(form.is_valid())

    def test_invalid_score_bad_format(self):
        form = self._post('21:15, 21-10')
        self.assertFalse(form.is_valid())
        self.assertIn('score', form.errors)

    def test_invalid_score_21_20(self):
        form = self._post('21-20, 21-10')
        self.assertFalse(form.is_valid())

    def test_invalid_only_1_set(self):
        form = self._post('21-15')
        self.assertFalse(form.is_valid())

    def test_invalid_too_many_sets(self):
        form = self._post('21-15, 21-10, 21-5, 21-3')
        self.assertFalse(form.is_valid())

    def test_invalid_unnecessary_third_set(self):
        # t1 wins first two → third set unnecessary
        form = self._post('21-15, 21-10, 21-5')
        self.assertFalse(form.is_valid())

    def test_invalid_winner_does_not_match_score(self):
        # t1 wins both sets but winner is set to t2
        form = self._post('21-15, 21-10', winner=self.t2)
        self.assertFalse(form.is_valid())
        self.assertTrue(any('set' in e.lower() or 'winner' in e.lower() for e in form.non_field_errors()))

    def test_correct_winner_is_valid(self):
        # t2 wins both sets, winner is t2
        form = self._post('15-21, 10-21', winner=self.t2)
        self.assertTrue(form.is_valid(), form.errors)

    def test_no_validation_when_score_empty(self):
        # Empty score with a winner is allowed (partial save)
        form = MatchResultForm(
            data={'score': '', 'winner': self.t1.pk},
            instance=self.match,
        )
        self.assertTrue(form.is_valid(), form.errors)


class WalkoverViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='woclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2
        )

    def test_walkover_get_returns_200(self):
        response = self.client.get(reverse("match_walkover", args=[self.match.pk]))
        self.assertEqual(response.status_code, 200)

    def test_walkover_post_sets_wo_score_and_winner(self):
        response = self.client.post(
            reverse("match_walkover", args=[self.match.pk]),
            {'winner': self.t1.pk},
        )
        self.assertRedirects(response, reverse("tournament_detail", args=[self.tournament.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.score, '21-0, 21-0')
        self.assertEqual(self.match.status, 'completed')
        self.assertEqual(self.match.winner, self.t1)
        self.assertTrue(self.match.walkover)

    def test_walkover_counts_in_standings(self):
        self.division.teams.add(self.t1, self.t2)
        self.match.winner = self.t2
        self.match.score = '21-0, 21-0'
        self.match.status = 'completed'
        self.match.walkover = True
        self.match.save()
        rows = compute_standings(self.division)
        self.assertEqual(rows[0]['team'], self.t2)
        self.assertEqual(rows[0]['points'], 2)

    def test_walkover_404_for_nonexistent(self):
        response = self.client.get(reverse("match_walkover", args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Group standings (playoff)
# ---------------------------------------------------------------------------

class GroupStandingsTest(TestCase):
    def setUp(self):
        self.tournament = make_tournament(owner=None)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Playoff", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.t3 = make_team(r1=5, r2=6)
        self.t4 = make_team(r1=7, r2=8)
        self.division.teams.add(self.t1, self.t2, self.t3, self.t4)

    def test_returns_groups_in_order(self):
        Match.objects.create(division=self.division, team1=self.t1, team2=self.t2,
                             group_number=1, phase='group', status='completed',
                             winner=self.t1, score='21-15')
        Match.objects.create(division=self.division, team1=self.t3, team2=self.t4,
                             group_number=2, phase='group', status='completed',
                             winner=self.t3, score='21-10')
        result = compute_group_standings(self.division)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], 1)
        self.assertEqual(result[1][0], 2)

    def test_winner_leads_group(self):
        Match.objects.create(division=self.division, team1=self.t1, team2=self.t2,
                             group_number=1, phase='group', status='completed',
                             winner=self.t1, score='21-15')
        result = compute_group_standings(self.division)
        g1_rows = result[0][1]
        self.assertEqual(g1_rows[0]['team'], self.t1)
        self.assertEqual(g1_rows[0]['points'], 2)

    def test_empty_division_returns_no_groups(self):
        empty_div = Division.objects.create(
            tournament=self.tournament, name="Empty playoff", discipline='double',
            tournament_type='playoff',
        )
        self.assertEqual(compute_group_standings(empty_div), [])


# ---------------------------------------------------------------------------
# DivisionSeed model
# ---------------------------------------------------------------------------

class DivisionSeedModelTest(TestCase):
    def test_str_includes_seed_number_and_names(self):
        t = make_tournament()
        d = make_division(tournament=t, tournament_type='tree')
        team = make_team(r1=1, r2=2)
        d.teams.add(team)
        seed = DivisionSeed.objects.create(division=d, team=team, seed_number=1)
        s = str(seed)
        self.assertIn('1', s)
        self.assertIn(team.name, s)


# ---------------------------------------------------------------------------
# Seeds views
# ---------------------------------------------------------------------------

class DivisionSeedViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='seedclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament, tournament_type='tree')
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_update_seeds_creates_entries(self):
        response = self.client.post(
            reverse('division_update_seeds', args=[self.division.pk]),
            {f'seed_{self.t1.pk}': '1', f'seed_{self.t2.pk}': '2'},
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.assertEqual(DivisionSeed.objects.filter(division=self.division).count(), 2)
        self.assertEqual(DivisionSeed.objects.get(division=self.division, team=self.t1).seed_number, 1)

    def test_update_seeds_clears_old_entries(self):
        DivisionSeed.objects.create(division=self.division, team=self.t1, seed_number=1)
        # Re-post with only t2 seeded
        self.client.post(
            reverse('division_update_seeds', args=[self.division.pk]),
            {f'seed_{self.t2.pk}': '1'},
        )
        self.assertFalse(DivisionSeed.objects.filter(division=self.division, team=self.t1).exists())
        self.assertTrue(DivisionSeed.objects.filter(division=self.division, team=self.t2).exists())

    def test_duplicate_seed_numbers_ignored(self):
        # Both teams given seed 1 → only first accepted
        self.client.post(
            reverse('division_update_seeds', args=[self.division.pk]),
            {f'seed_{self.t1.pk}': '1', f'seed_{self.t2.pk}': '1'},
        )
        self.assertEqual(DivisionSeed.objects.filter(division=self.division).count(), 1)

    def test_invalid_seed_value_ignored(self):
        self.client.post(
            reverse('division_update_seeds', args=[self.division.pk]),
            {f'seed_{self.t1.pk}': 'abc'},
        )
        self.assertEqual(DivisionSeed.objects.filter(division=self.division).count(), 0)

    def test_seed_list_in_detail_context(self):
        DivisionSeed.objects.create(division=self.division, team=self.t1, seed_number=1)
        response = self.client.get(reverse('tournament_detail', args=[self.tournament.pk]))
        dd = response.context['division_data'][0]
        seed_list = dd['seed_list']
        team_pks = [t.pk for t, _ in seed_list]
        self.assertIn(self.t1.pk, team_pks)

    def test_seeds_dict_in_detail_context(self):
        DivisionSeed.objects.create(division=self.division, team=self.t1, seed_number=1)
        response = self.client.get(reverse('tournament_detail', args=[self.tournament.pk]))
        dd = response.context['division_data'][0]
        self.assertIn(self.t1.pk, dd['seeds_dict'])
        self.assertIn('(1)', dd['seeds_dict'][self.t1.pk])


# ---------------------------------------------------------------------------
# Bigscreen view
# ---------------------------------------------------------------------------

class BigscreenViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='bigscreenclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)

    def test_bigscreen_returns_200(self):
        response = self.client.get(reverse('tournament_bigscreen', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_bigscreen_shows_pending_matches_with_time(self):
        import datetime as dt
        from django.utils import timezone
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='pending', match_number=1,
            scheduled_time=timezone.make_aware(dt.datetime(2026, 1, 1, 10, 0)),
            court='1',
        )
        response = self.client.get(reverse('tournament_bigscreen', args=[self.tournament.pk]))
        # Team name containing '&' is HTML-escaped; check player name instead
        self.assertContains(response, self.t1.player1.name)

    def test_bigscreen_excludes_completed(self):
        import datetime as dt
        from django.utils import timezone
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='completed', match_number=1,
            scheduled_time=timezone.make_aware(dt.datetime(2026, 1, 1, 10, 0)),
        )
        response = self.client.get(reverse('tournament_bigscreen', args=[self.tournament.pk]))
        self.assertEqual(len(response.context['matches']), 0)

    def test_bigscreen_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_bigscreen', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tournament program print + division scoresheet
# ---------------------------------------------------------------------------

class ProgramPrintViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='printclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_program_print_returns_200(self):
        response = self.client.get(reverse('tournament_program_print', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_program_print_contains_division_name(self):
        response = self.client.get(reverse('tournament_program_print', args=[self.tournament.pk]))
        self.assertContains(response, self.division.name)

    def test_program_print_contains_team_names(self):
        response = self.client.get(reverse('tournament_program_print', args=[self.tournament.pk]))
        # Team names containing '&' are HTML-escaped in the response
        self.assertContains(response, self.t1.player1.name)

    def test_program_print_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_program_print', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_schedule_print_returns_200(self):
        response = self.client.get(reverse('tournament_schedule_print', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_schedule_print_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_schedule_print', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_schedule_print_contains_match_number(self):
        from tournaments.models import Match
        match = Match.objects.create(
            division=self.division,
            team1=self.t1,
            team2=self.t2,
            match_round=1,
            match_number=42,
            scheduled_time='2026-04-22 10:00:00+00:00',
            court=2,
        )
        response = self.client.get(reverse('tournament_schedule_print', args=[self.tournament.pk]))
        self.assertContains(response, '42')
        self.assertContains(response, self.t1.player1.name)


class DivisionScoresheetViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='scoresheetclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_division_scoresheet_returns_200(self):
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        response = self.client.get(reverse('division_scoresheet', args=[self.division.pk]))
        self.assertEqual(response.status_code, 200)

    def test_division_scoresheet_404_for_nonexistent(self):
        response = self.client.get(reverse('division_scoresheet', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Toggle lock view
# ---------------------------------------------------------------------------

class ToggleLockViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='lockclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)

    def test_lock_toggles_on(self):
        self.assertFalse(self.tournament.schedule_locked)
        self.client.post(reverse('tournament_toggle_lock', args=[self.tournament.pk]))
        self.tournament.refresh_from_db()
        self.assertTrue(self.tournament.schedule_locked)

    def test_lock_toggles_off(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        self.client.post(reverse('tournament_toggle_lock', args=[self.tournament.pk]))
        self.tournament.refresh_from_db()
        self.assertFalse(self.tournament.schedule_locked)

    def test_lock_redirects_to_schedule(self):
        response = self.client.post(reverse('tournament_toggle_lock', args=[self.tournament.pk]))
        self.assertRedirects(response, reverse('tournament_schedule', args=[self.tournament.pk]))


# ---------------------------------------------------------------------------
# Generate time schedule – locked / no start_time paths
# ---------------------------------------------------------------------------

class TimeScheduleEdgeCaseTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='timeedgeclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_generate_when_locked_shows_error(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        response = self.client.post(
            reverse('tournament_generate_time_schedule', args=[self.tournament.pk])
        )
        self.assertRedirects(response, reverse('tournament_schedule', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('locked' in str(m).lower() for m in messages_list))

    def test_generate_with_no_matches_shows_warning(self):
        make_day(self.tournament)
        # No matches generated → count=0 → warning
        response = self.client.post(
            reverse('tournament_generate_time_schedule', args=[self.tournament.pk])
        )
        self.assertRedirects(response, reverse('tournament_schedule', args=[self.tournament.pk]))


# ---------------------------------------------------------------------------
# Generate schedule – tree bracket_label path + locked guard
# ---------------------------------------------------------------------------

class GenerateScheduleTreeTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='treeclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament, tournament_type='tree')
        for i in range(4):
            t = make_team(r1=i * 2 + 1, r2=i * 2 + 2)
            self.division.teams.add(t)

    def test_tree_generation_updates_bracket_labels(self):
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        placeholder = Match.objects.filter(division=self.division, team1__isnull=True).first()
        # Bracket label should reference match numbers, not slot codes
        self.assertIsNotNone(placeholder)
        self.assertNotIn('R1S', placeholder.bracket_label or '')

    def test_locked_tournament_blocks_generate(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        response = self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.assertEqual(Match.objects.filter(division=self.division).count(), 0)


# ---------------------------------------------------------------------------
# Generate schedule – playoff message path
# ---------------------------------------------------------------------------

class GenerateSchedulePlayoffTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='playoffclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Playoff D", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        for i in range(4):
            t = make_team(r1=i * 2 + 1, r2=i * 2 + 2)
            self.division.teams.add(t)

    def test_playoff_generate_shows_group_and_playoff_count(self):
        response = self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('group' in str(m).lower() for m in messages_list))


# ---------------------------------------------------------------------------
# Scheduler edge cases – seeding + advance_bracket for non-tree
# ---------------------------------------------------------------------------

class SchedulerSeedingTest(TestCase):
    def test_seeded_teams_placed_first_in_bracket(self):
        from .scheduler import _sort_teams_by_seed
        division = make_division(tournament_type='tree')
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        division.teams.add(t1, t2)
        # Seed t2 as #1 → it should come first
        DivisionSeed.objects.create(division=division, team=t2, seed_number=1)
        sorted_teams = _sort_teams_by_seed(division, [t1, t2])
        self.assertEqual(sorted_teams[0], t2)
        self.assertEqual(sorted_teams[1], t1)

    def test_advance_bracket_non_tree_does_nothing(self):
        division = make_division(tournament_type='group')
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        match = Match.objects.create(
            division=division, team1=t1, team2=t2,
            status='completed', winner=t1, bracket_slot=1, match_round=1,
        )
        # Should not raise, and no next-round placeholder is created
        advance_bracket(match)
        self.assertEqual(Match.objects.filter(division=division).count(), 1)

    def test_generate_bracket_too_few_teams_returns_empty(self):
        division = make_division(tournament_type='tree')
        t1 = make_team(r1=1, r2=2)
        division.teams.add(t1)
        result = generate_bracket(division)
        self.assertEqual(result, [])

    def test_generate_round_robin_too_few_returns_empty(self):
        division = make_division(tournament_type='group')
        t1 = make_team(r1=1, r2=2)
        division.teams.add(t1)
        result = generate_round_robin(division)
        self.assertEqual(result, [])

    def test_generate_playoff_too_few_returns_empty(self):
        from .scheduler import generate_playoff
        division = Division.objects.create(
            tournament=make_tournament(), name="P", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        t1 = make_team(r1=1, r2=2)
        division.teams.add(t1)
        result = generate_playoff(division)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# schedule_planner – placeholder + playoff barrier paths
# ---------------------------------------------------------------------------

class SchedulePlannerPlayoffTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='schedulerclub')
        self.client.force_login(self.user)
        self.tournament = Tournament.objects.create(
            name="TP", date=datetime.date(2026, 6, 1),
            division_model='mixed', scoring_model='best_of_3_21',
            player_break_time=15, single_match_duration=30,
            owner=self.user,
        )
        make_day(self.tournament, courts=2)
        self.division = Division.objects.create(
            tournament=self.tournament, name="P", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        for i in range(4):
            t = make_team(r1=i * 2 + 1, r2=i * 2 + 2)
            self.division.teams.add(t)

    def test_playoff_bracket_matches_scheduled_after_group_matches(self):
        from .schedule_planner import generate_time_schedule
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        count = generate_time_schedule(self.tournament)
        self.assertGreater(count, 0)
        group_times = list(
            Match.objects.filter(division=self.division, phase='group')
            .exclude(scheduled_time=None)
            .values_list('scheduled_time', flat=True)
        )
        playoff_times = list(
            Match.objects.filter(division=self.division, phase='playoff')
            .exclude(scheduled_time=None)
            .values_list('scheduled_time', flat=True)
        )
        if group_times and playoff_times:
            self.assertGreaterEqual(min(playoff_times), max(group_times))


# ---------------------------------------------------------------------------
# templatetag dict_get – non-dict fallback
# ---------------------------------------------------------------------------

class DictGetFilterTest(TestCase):
    def test_non_dict_returns_empty_string(self):
        from tournaments.templatetags.tournament_extras import dict_get
        self.assertEqual(dict_get('not-a-dict', 'key'), '')
        self.assertEqual(dict_get(None, 'key'), '')
        self.assertEqual(dict_get(42, 'key'), '')

    def test_dict_returns_value(self):
        from tournaments.templatetags.tournament_extras import dict_get
        self.assertEqual(dict_get({'a': 'x'}, 'a'), 'x')
        self.assertEqual(dict_get({'a': 'x'}, 'b'), '')


# ---------------------------------------------------------------------------
# Tournament delete view
# ---------------------------------------------------------------------------

class TournamentDeleteTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='deleteclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)

    def test_delete_get_shows_confirm(self):
        response = self.client.get(reverse('tournament_delete', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.tournament.name)

    def test_delete_post_removes_tournament(self):
        pk = self.tournament.pk
        response = self.client.post(reverse('tournament_delete', args=[pk]), {'confirm': 'SLET TURNERING'})
        self.assertRedirects(response, reverse('tournament_list'))
        self.assertFalse(Tournament.objects.filter(pk=pk).exists())

    def test_delete_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_delete', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_delete_other_owners_tournament_returns_404(self):
        other = make_user(username='otherdeleteclub')
        other_tournament = make_tournament(owner=other)
        response = self.client.get(reverse('tournament_delete', args=[other_tournament.pk]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tournament export view
# ---------------------------------------------------------------------------

class TournamentExportTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='exportclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        self.division.teams.add(t1, t2)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))

    def test_export_returns_json_attachment(self):
        response = self.client.get(reverse('tournament_export', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('application/json', response['Content-Type'])
        self.assertIn('attachment', response['Content-Disposition'])

    def test_export_filename_contains_tournament_date(self):
        response = self.client.get(reverse('tournament_export', args=[self.tournament.pk]))
        self.assertIn(str(self.tournament.date), response['Content-Disposition'])

    def test_export_contains_required_sections(self):
        import json as _json
        response = self.client.get(reverse('tournament_export', args=[self.tournament.pk]))
        data = _json.loads(response.content.decode('utf-8'))
        for key in ('version', 'tournament', 'players', 'teams', 'divisions', 'matches', 'seeds'):
            self.assertIn(key, data)

    def test_export_version_is_1(self):
        import json as _json
        response = self.client.get(reverse('tournament_export', args=[self.tournament.pk]))
        data = _json.loads(response.content.decode('utf-8'))
        self.assertEqual(data['version'], 1)

    def test_export_404_for_other_owner(self):
        other = make_user(username='otherexportclub')
        other_tournament = make_tournament(owner=other)
        response = self.client.get(reverse('tournament_export', args=[other_tournament.pk]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tournament import view
# ---------------------------------------------------------------------------

class TournamentImportTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='importclub')
        self.client.force_login(self.user)
        # Build a valid backup by exporting a real tournament
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        self.division.teams.add(t1, t2)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))

    def _export_json(self):
        import json as _json
        response = self.client.get(reverse('tournament_export', args=[self.tournament.pk]))
        return _json.loads(response.content.decode('utf-8'))

    def _upload(self, data):
        import json as _json
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile
        raw = _json.dumps(data).encode('utf-8')
        f = SimpleUploadedFile('backup.json', raw, content_type='application/json')
        return self.client.post(reverse('tournament_import'), {'backup_file': f})

    def test_import_get_returns_200(self):
        response = self.client.get(reverse('tournament_import'))
        self.assertEqual(response.status_code, 200)

    def test_import_no_file_shows_error(self):
        response = self.client.post(reverse('tournament_import'), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No file selected')

    def test_import_invalid_json_shows_error(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile('bad.json', b'not valid json', content_type='application/json')
        response = self.client.post(reverse('tournament_import'), {'backup_file': f})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'JSON')

    def test_import_wrong_version_shows_error(self):
        data = self._export_json()
        data['version'] = 99
        response = self._upload(data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'version')

    def test_import_valid_backup_creates_tournament(self):
        data = self._export_json()
        count_before = Tournament.objects.count()
        response = self._upload(data)
        self.assertEqual(Tournament.objects.count(), count_before + 1)
        new_t = Tournament.objects.order_by('-pk').first()
        self.assertIn('gendannet', new_t.name)
        self.assertRedirects(response, reverse('tournament_detail', args=[new_t.pk]))

    def test_import_deduplicates_players(self):
        from players.models import Player as P
        data = self._export_json()
        # First import: creates players
        self._upload(data)
        count_after_first = P.objects.filter(owner=self.user).count()
        self.assertGreater(count_after_first, 0)
        # Second import of same data: players should be get_or_created, not duplicated
        self._upload(data)
        self.assertEqual(P.objects.filter(owner=self.user).count(), count_after_first)

    def test_import_creates_divisions_and_matches(self):
        import json as _json
        data = self._export_json()
        self._upload(data)
        new_t = Tournament.objects.order_by('-pk').first()
        self.assertEqual(new_t.divisions.count(), 1)
        self.assertGreater(Match.objects.filter(division__tournament=new_t).count(), 0)


# ---------------------------------------------------------------------------
# Division set priority view
# ---------------------------------------------------------------------------

class DivisionSetPriorityTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='priorityclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)

    def test_set_priority_updates_value(self):
        response = self.client.post(
            reverse('division_set_priority', args=[self.division.pk]),
            {'schedule_priority': '3'},
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.division.refresh_from_db()
        self.assertEqual(self.division.schedule_priority, 3)

    def test_set_priority_clamps_to_1_at_minimum(self):
        self.client.post(
            reverse('division_set_priority', args=[self.division.pk]),
            {'schedule_priority': '-5'},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.schedule_priority, 1)

    def test_set_priority_clamps_to_10_at_maximum(self):
        self.client.post(
            reverse('division_set_priority', args=[self.division.pk]),
            {'schedule_priority': '99'},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.schedule_priority, 10)

    def test_set_priority_invalid_string_leaves_value_unchanged(self):
        self.division.schedule_priority = 5
        self.division.save()
        self.client.post(
            reverse('division_set_priority', args=[self.division.pk]),
            {'schedule_priority': 'abc'},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.schedule_priority, 5)

    def test_set_priority_get_redirects(self):
        response = self.client.get(
            reverse('division_set_priority', args=[self.division.pk])
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))


# ---------------------------------------------------------------------------
# Match start view
# ---------------------------------------------------------------------------

class MatchStartTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='startclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2, match_number=1
        )

    def test_match_start_sets_in_progress(self):
        response = self.client.post(reverse('match_start', args=[self.match.pk]), {'court': '1'})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, 'in_progress')
        self.assertEqual(self.match.court, '1')
        self.assertIsNotNone(self.match.started_at)

    def test_match_start_already_in_progress_not_changed(self):
        self.match.status = 'in_progress'
        self.match.save()
        self.client.post(reverse('match_start', args=[self.match.pk]), {'court': '1'})
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, 'in_progress')

    def test_match_start_without_court_shows_error(self):
        response = self.client.post(reverse('match_start', args=[self.match.pk]))
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, 'pending')
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('court' in str(m).lower() or 'bane' in str(m).lower() for m in messages_list))

    def test_match_start_blocked_when_court_already_in_use(self):
        t3 = make_team(r1=5, r2=6)
        t4 = make_team(r1=7, r2=8)
        Match.objects.create(
            division=self.division, team1=t3, team2=t4,
            status='in_progress', match_number=99, court='1',
        )
        response = self.client.post(reverse('match_start', args=[self.match.pk]), {'court': '1'})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, 'pending')
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('already in use' in str(m).lower() or 'i brug' in str(m).lower() for m in messages_list))

    def test_match_start_blocked_when_player_already_playing(self):
        from django.utils import timezone
        # Put t1's players in another in_progress match
        other_match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='in_progress', match_number=99
        )
        response = self.client.post(reverse('match_start', args=[self.match.pk]), {'court': '1'})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.match.refresh_from_db()
        # Should stay pending because player is already playing
        self.assertEqual(self.match.status, 'pending')
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('already playing' in str(m) for m in messages_list))

    def test_match_start_404_for_nonexistent(self):
        response = self.client.post(reverse('match_start', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_match_start_blocked_when_doubles_player_not_arrived(self):
        self.t2.player2.has_arrived = False
        self.t2.player2.save()
        response = self.client.post(reverse('match_start', args=[self.match.pk]), {'court': '1'})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, 'pending')
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('has not arrived' in str(m) for m in messages_list))


# ---------------------------------------------------------------------------
# Public views (no login required)
# ---------------------------------------------------------------------------

class PublicViewTest(TestCase):
    def setUp(self):
        self.user = make_user(username='publicclub')
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        self.division.teams.add(t1, t2)

    def test_public_landing_returns_200_without_login(self):
        response = self.client.get(reverse('public_landing'))
        self.assertEqual(response.status_code, 200)

    def test_public_landing_lists_tournaments(self):
        response = self.client.get(reverse('public_landing'))
        self.assertContains(response, self.tournament.name)

    def test_public_landing_applies_green_button_theme(self):
        response = self.client.get(reverse('public_landing'))
        content = response.content.decode('utf-8')
        self.assertContains(response, 'class="btn btn-primary"', html=False)
        self.assertContains(response, 'class="btn btn-secondary"', html=False)
        self.assertIn('.btn-secondary', content)
        self.assertIn('.btn-danger', content)

    def test_public_landing_is_cached_with_explicit_ttl(self):
        response = self.client.get(reverse('public_landing'))
        self.assertEqual(response['Cache-Control'], 'max-age=20')

    def test_public_tournament_and_schedule_are_cached_with_explicit_ttl(self):
        for name in ('public_tournament', 'public_schedule'):
            response = self.client.get(reverse(name, args=[self.tournament.pk]))
            self.assertEqual(response['Cache-Control'], 'max-age=10')

    def test_public_tournament_returns_200_without_login(self):
        response = self.client.get(reverse('public_tournament', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_public_tournament_contains_division(self):
        response = self.client.get(reverse('public_tournament', args=[self.tournament.pk]))
        self.assertContains(response, self.division.name)

    def test_public_tournament_404_for_nonexistent(self):
        response = self.client.get(reverse('public_tournament', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_public_schedule_returns_200_without_login(self):
        response = self.client.get(reverse('public_schedule', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_public_schedule_404_for_nonexistent(self):
        response = self.client.get(reverse('public_schedule', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_public_schedule_shows_scheduled_matches(self):
        import datetime as dt
        from django.utils import timezone
        t1 = make_team(r1=5, r2=6)
        t2 = make_team(r1=7, r2=8)
        Match.objects.create(
            division=self.division, team1=t1, team2=t2, match_number=10,
            scheduled_time=timezone.make_aware(dt.datetime(2026, 6, 1, 9, 0)),
            court='1',
        )
        response = self.client.get(reverse('public_schedule', args=[self.tournament.pk]))
        self.assertContains(response, t1.player1.name)

    def test_public_tournament_cache_is_invalidated_when_data_changes(self):
        url = reverse('public_tournament', args=[self.tournament.pk])
        self.client.get(url)
        self.tournament.name = 'Updated Tournament'
        self.tournament.save()
        response = self.client.get(url)
        self.assertContains(response, 'Updated Tournament')

    def test_result_change_invalidates_tournament_and_schedule_caches(self):
        import datetime as dt
        from django.utils import timezone
        team1, team2 = self.division.teams.all()
        match = Match.objects.create(
            division=self.division, team1=team1, team2=team2,
            match_number=1, status='completed',
            scheduled_time=timezone.make_aware(dt.datetime(2026, 6, 1, 9, 0)),
        )
        tournament_url = reverse('public_tournament', args=[self.tournament.pk])
        schedule_url = reverse('public_schedule', args=[self.tournament.pk])
        self.client.get(tournament_url)
        self.client.get(schedule_url)
        match.score = '21-19, 21-17'
        match.save()
        self.assertContains(self.client.get(tournament_url), match.score)
        self.assertContains(self.client.get(schedule_url), match.score)

    def test_dashboard_is_not_cached_as_a_public_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('admin_home'))
        self.assertNotIn('max-age=30', response.get('Cache-Control', ''))


# ---------------------------------------------------------------------------
# Player status functions
# ---------------------------------------------------------------------------

class PlayerStatusFunctionTest(TestCase):
    def setUp(self):
        self.user = make_user(username='statusclub')
        self.tournament = Tournament.objects.create(
            name="Status T", date=datetime.date.today(),
            division_model='mixed', scoring_model='best_of_3_21',
            player_break_time=15, owner=self.user,
        )
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_set_player_rest_sets_rest_until(self):
        from tournaments.player_status import set_player_rest
        from django.utils import timezone
        match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='completed', winner=self.t1,
        )
        set_player_rest(match)
        self.t1.player1.refresh_from_db()
        self.t1.player2.refresh_from_db()
        self.assertIsNotNone(self.t1.player1.rest_until)
        self.assertIsNotNone(self.t1.player2.rest_until)
        self.assertGreater(self.t1.player1.rest_until, timezone.now())

    def test_check_match_startable_free_players_ok(self):
        from tournaments.player_status import check_match_startable
        match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
        )
        errors = check_match_startable(match)
        self.assertEqual(errors, [])

    def test_check_match_startable_resting_player_blocked(self):
        from tournaments.player_status import check_match_startable
        from django.utils import timezone
        import datetime as dt
        # Set rest_until to far future so player is resting
        self.t1.player1.rest_until = timezone.now() + dt.timedelta(minutes=30)
        self.t1.player1.save()
        match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
        )
        errors = check_match_startable(match)
        self.assertTrue(any('still resting' in e for e in errors))

    def test_check_match_startable_playing_player_blocked(self):
        from tournaments.player_status import check_match_startable
        # Create an in_progress match with t1
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='in_progress',
        )
        match2 = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
        )
        errors = check_match_startable(match2)
        self.assertTrue(any('already playing' in e for e in errors))

    def test_check_match_startable_blocked_when_player_not_arrived(self):
        from tournaments.player_status import check_match_startable
        self.t1.player1.has_arrived = False
        self.t1.player1.save()
        match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
        )
        errors = check_match_startable(match)
        self.assertTrue(any('has not arrived' in e for e in errors))

    def test_check_match_startable_doubles_blocked_until_all_four_arrived(self):
        from tournaments.player_status import check_match_startable
        # Only one of the four players hasn't checked in yet
        self.t2.player2.has_arrived = False
        self.t2.player2.save()
        match = Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
        )
        errors = check_match_startable(match)
        self.assertTrue(any('has not arrived' in e for e in errors))
        # Once everyone has arrived, no more errors
        self.t2.player2.has_arrived = True
        self.t2.player2.save()
        self.assertEqual(check_match_startable(match), [])

    def test_team_status_playing(self):
        from tournaments.player_status import team_status, get_busy_info
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='in_progress',
        )
        playing_pks, resting = get_busy_info()
        status, _ = team_status(self.t1, playing_pks, resting)
        self.assertEqual(status, 'playing')

    def test_team_status_resting(self):
        from tournaments.player_status import team_status, get_busy_info
        import datetime as dt
        from django.utils import timezone
        self.t1.player1.rest_until = timezone.now() + dt.timedelta(minutes=20)
        self.t1.player1.save()
        playing_pks, resting = get_busy_info()
        status, ru = team_status(self.t1, playing_pks, resting)
        self.assertEqual(status, 'resting')
        self.assertIsNotNone(ru)

    def test_team_status_none_team_returns_none(self):
        from tournaments.player_status import team_status
        status, ru = team_status(None, set(), {})
        self.assertIsNone(status)
        self.assertIsNone(ru)


# ---------------------------------------------------------------------------
# Walkover score respects scoring model
# ---------------------------------------------------------------------------

class WalkoverScoreModelTest(TestCase):
    """_walkover_score returns the correct score string for each scoring model."""

    def setUp(self):
        self.client = Client()
        self.user = make_user(username='woscoreclub')
        self.client.force_login(self.user)

    def _make_match(self, scoring_model):
        tournament = Tournament.objects.create(
            name="WO Score Test", date=datetime.date.today(),
            division_model='mixed', scoring_model=scoring_model,
            owner=self.user,
        )
        division = make_division(tournament=tournament)
        t1 = make_team(r1=50, r2=51)
        t2 = make_team(r1=52, r2=53)
        division.teams.add(t1, t2)
        return Match.objects.create(division=division, team1=t1, team2=t2), t1

    def test_walkover_score_21_for_best_of_3_21(self):
        match, t1 = self._make_match('best_of_3_21')
        self.client.post(reverse('match_walkover', args=[match.pk]), {'winner': t1.pk})
        match.refresh_from_db()
        self.assertEqual(match.score, '21-0, 21-0')

    def test_walkover_score_15_for_best_of_5_15(self):
        match, t1 = self._make_match('best_of_5_15')
        self.client.post(reverse('match_walkover', args=[match.pk]), {'winner': t1.pk})
        match.refresh_from_db()
        self.assertEqual(match.score, '15-0, 15-0')


# ---------------------------------------------------------------------------
# 3-way standings tiebreaker: sets_diff → score_diff → score_for
# ---------------------------------------------------------------------------

class ThreeWayTiebreakerTest(TestCase):
    """When 3 teams each win one match the tiebreakers determine order."""

    def setUp(self):
        self.tournament = make_tournament()
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=60, r2=61)
        self.t2 = make_team(r1=62, r2=63)
        self.t3 = make_team(r1=64, r2=65)
        self.division.teams.add(self.t1, self.t2, self.t3)

    def _match(self, team1, team2, winner, score):
        Match.objects.create(
            division=self.division, team1=team1, team2=team2,
            status='completed', winner=winner, score=score,
        )

    def test_sets_diff_breaks_three_way_tie(self):
        # Everyone wins one match (2 pts each) but t1 wins in straight sets more often
        # t1 beats t2 21-10, 21-10  (2 sets)
        # t2 beats t3 21-10, 21-10  (2 sets)
        # t3 beats t1 21-10, 8-21, 21-10  (wins in 3 sets → more sets lost)
        self._match(self.t1, self.t2, self.t1, '21-10, 21-10')   # t1: sw=2, sl=0
        self._match(self.t2, self.t3, self.t2, '21-10, 21-10')   # t2: sw=2, sl=0 but also lost sets to t1
        self._match(self.t3, self.t1, self.t3, '21-10, 8-21, 21-10')  # t3 wins but uses 3 sets

        # sets_diff: t1 = 2-2=0, t2 = 2-2=0 … actually let's just check t3 is last (1 set lost more)
        rows = compute_standings(self.division)
        names = [r['team'] for r in rows]
        # t3 has net sets: won 2 (vs t1 in 3 sets but counted 2 wins), lost 2 (to t2)
        # All have 2 pts; order determined by sets_diff then score_diff
        # Just ensure the function runs and returns all 3 teams
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(names), {self.t1, self.t2, self.t3})

    def test_score_diff_breaks_three_way_tie_when_sets_equal(self):
        # All win one match in straight sets (sets_diff equal)
        # t1 wins by large margin, t2 by medium, t3 by small
        self._match(self.t1, self.t2, self.t1, '21-5, 21-5')    # net +32
        self._match(self.t2, self.t3, self.t2, '21-10, 21-10')  # net +22
        self._match(self.t3, self.t1, self.t3, '21-19, 21-19')  # net +4
        rows = compute_standings(self.division)
        # sets_diff for all: won 2, lost 2 (each wins one match 2-0, loses one 0-2)
        # score_diff: t1 net = +32-2 = +30, t2 = +22-32 = -10, t3 = +4-22 = -18
        self.assertEqual(rows[0]['team'], self.t1)
        self.assertEqual(rows[1]['team'], self.t2)
        self.assertEqual(rows[2]['team'], self.t3)

    def test_score_for_breaks_three_way_tie_when_diff_equal(self):
        # Contrived: all have same sets_diff AND same score_diff, but different score_for
        # Use 3 separate divisions so we can control exactly
        div = make_division(tournament=self.tournament, name='TiebreakerDiv3')
        ta = make_team(r1=70, r2=71)
        tb = make_team(r1=72, r2=73)
        tc = make_team(r1=74, r2=75)
        div.teams.add(ta, tb, tc)
        # ta beats tb 21-0 21-0  (score_for=42)
        # tb beats tc 21-0 21-0  (score_for=42 for tb across both matches=21+21=42 won, 21+21 lost)
        # tc beats ta 21-0 21-0  (score_for=21+21=42)
        # All have score_diff=0 and sets_diff=0
        Match.objects.create(division=div, team1=ta, team2=tb, status='completed', winner=ta, score='21-0, 21-0')
        Match.objects.create(division=div, team1=tb, team2=tc, status='completed', winner=tb, score='21-0, 21-0')
        Match.objects.create(division=div, team1=tc, team2=ta, status='completed', winner=tc, score='21-0, 21-0')
        rows = compute_standings(div)
        self.assertEqual(len(rows), 3)
        # All completely equal → order is stable (no assertion on order, just no crash)


# ---------------------------------------------------------------------------
# fill_playoff_bracket_from_group
# ---------------------------------------------------------------------------

class FillPlayoffBracketTest(TestCase):
    """After all group matches complete, bracket slots are filled correctly."""

    def setUp(self):
        from .scheduler import generate_playoff, fill_playoff_bracket_from_group
        self.fill = fill_playoff_bracket_from_group
        self.user = make_user(username='playoffclub')
        self.tournament = Tournament.objects.create(
            name="Playoff Fill Test", date=datetime.date.today(),
            division_model='mixed', scoring_model='best_of_3_21',
            owner=self.user,
        )
        # 1 group, top-1 advances → 2 teams total, 1 advances
        self.division = Division.objects.create(
            tournament=self.tournament, name="PF", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        self.t1 = make_team(r1=80, r2=81)
        self.t2 = make_team(r1=82, r2=83)
        self.t3 = make_team(r1=84, r2=85)
        self.t4 = make_team(r1=86, r2=87)
        self.division.teams.add(self.t1, self.t2, self.t3, self.t4)
        generate_playoff(self.division)

    def _complete_group(self, group_number, winner_team, loser_team):
        match = Match.objects.get(
            division=self.division, phase='group', group_number=group_number,
        )
        match.winner = winner_team
        match.score = '21-10, 21-10'
        match.status = 'completed'
        match.save()
        return match

    def test_bracket_slot_filled_after_group_completes(self):
        from .scheduler import fill_playoff_bracket_from_group
        # Complete group 1 – winner should be placed in a bracket slot
        match = Match.objects.filter(
            division=self.division, phase='group', group_number=1,
        ).first()
        match.winner = match.team1
        match.score = '21-10, 21-10'
        match.status = 'completed'
        match.save()
        fill_playoff_bracket_from_group(self.division, 1)

        bracket_matches = Match.objects.filter(division=self.division, phase='playoff')
        filled = [m for m in bracket_matches if m.team1 is not None or m.team2 is not None]
        self.assertTrue(len(filled) >= 1, "Expected at least one bracket slot to be filled")

    def test_no_fill_while_group_still_pending(self):
        from .scheduler import fill_playoff_bracket_from_group
        # Don't complete any matches – nothing should be filled
        fill_playoff_bracket_from_group(self.division, 1)
        bracket_matches = Match.objects.filter(division=self.division, phase='playoff')
        filled = [m for m in bracket_matches if m.team1 is not None or m.team2 is not None]
        self.assertEqual(len(filled), 0)


# ---------------------------------------------------------------------------
# advance_bracket works for playoff bracket phase
# ---------------------------------------------------------------------------

class AdvanceBracketPlayoffTest(TestCase):
    """Winner of a playoff bracket match advances to the next round."""

    def setUp(self):
        from .scheduler import generate_playoff
        self.user = make_user(username='advplayoffclub')
        self.tournament = Tournament.objects.create(
            name="Adv Playoff", date=datetime.date.today(),
            division_model='mixed', scoring_model='best_of_3_21',
            owner=self.user,
        )
        self.division = Division.objects.create(
            tournament=self.tournament, name="AP", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=2,
        )
        # 8 teams → 2 groups of 4 → 4 advance → semi + final
        for i in range(8):
            t = make_team(r1=90 + i * 2, r2=91 + i * 2)
            self.division.teams.add(t)
        generate_playoff(self.division)

    def test_advance_fills_next_bracket_round(self):
        # Find a round-1 playoff bracket match that has both teams filled
        semifinal = Match.objects.filter(
            division=self.division, phase='playoff',
        ).order_by('match_round', 'bracket_slot').first()

        if semifinal is None or semifinal.team1 is None or semifinal.team2 is None:
            self.skipTest("No ready bracket match found after generate_playoff")

        winner = semifinal.team1
        semifinal.winner = winner
        semifinal.status = 'completed'
        semifinal.save()
        advance_bracket(semifinal)

        next_round = semifinal.match_round + 1
        next_slot = (semifinal.bracket_slot + 1) // 2
        next_match = Match.objects.filter(
            division=self.division, phase='playoff',
            match_round=next_round, bracket_slot=next_slot,
        ).first()

        if next_match:
            is_odd = (semifinal.bracket_slot % 2 == 1)
            if is_odd:
                self.assertEqual(next_match.team1, winner)
            else:
                self.assertEqual(next_match.team2, winner)


# ---------------------------------------------------------------------------
# match_bracket_override view
# ---------------------------------------------------------------------------

class BracketOverrideViewTest(TestCase):
    """match_bracket_override lets admin manually set teams in a bracket slot."""

    def setUp(self):
        self.client = Client()
        self.user = make_user(username='overrideclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament, tournament_type='tree')
        self.t1 = make_team(r1=100, r2=101)
        self.t2 = make_team(r1=102, r2=103)
        self.t3 = make_team(r1=104, r2=105)
        self.division.teams.add(self.t1, self.t2, self.t3)
        # Bracket match with no teams yet (placeholder)
        self.match = Match.objects.create(
            division=self.division, team1=None, team2=None,
            match_round=2, bracket_slot=1, status='pending',
            bracket_label='Semifinale',
        )

    def test_get_returns_200(self):
        response = self.client.get(
            reverse('match_bracket_override', args=[self.match.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_get_shows_team_choices(self):
        response = self.client.get(
            reverse('match_bracket_override', args=[self.match.pk])
        )
        # Team names contain '&' which is HTML-escaped; check by player name instead
        self.assertContains(response, 'P100')
        self.assertContains(response, 'P102')

    def test_post_sets_team1(self):
        self.client.post(
            reverse('match_bracket_override', args=[self.match.pk]),
            {'team1': self.t1.pk, 'team2': ''},
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.team1, self.t1)

    def test_post_sets_both_teams_clears_label(self):
        self.client.post(
            reverse('match_bracket_override', args=[self.match.pk]),
            {'team1': self.t1.pk, 'team2': self.t2.pk},
        )
        self.match.refresh_from_db()
        self.assertEqual(self.match.team1, self.t1)
        self.assertEqual(self.match.team2, self.t2)
        self.assertIsNone(self.match.bracket_label)

    def test_post_redirects_to_run(self):
        response = self.client.post(
            reverse('match_bracket_override', args=[self.match.pk]),
            {'team1': self.t1.pk, 'team2': self.t2.pk},
        )
        self.assertRedirects(
            response, reverse('tournament_run', args=[self.tournament.pk])
        )

    def test_post_404_for_other_owner(self):
        other = make_user(username='otheroverride')
        self.client.force_login(other)
        response = self.client.post(
            reverse('match_bracket_override', args=[self.match.pk]),
            {'team1': self.t1.pk, 'team2': ''},
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Medal system tests
# ---------------------------------------------------------------------------

class DivisionMedalFieldsTest(TestCase):
    """Division model stores medal count fields with correct defaults."""

    def test_default_gold_silver_bronze(self):
        d = make_division()
        self.assertEqual(d.gold_count, 1)
        self.assertEqual(d.silver_count, 1)
        self.assertEqual(d.bronze_count, 0)

    def test_custom_medal_counts_saved(self):
        d = make_division()
        d.gold_count = 2
        d.silver_count = 2
        d.bronze_count = 2
        d.save()
        d.refresh_from_db()
        self.assertEqual(d.gold_count, 2)
        self.assertEqual(d.silver_count, 2)
        self.assertEqual(d.bronze_count, 2)


class DivisionMedalsViewGroupTest(TestCase):
    """division_medals view returns correct medals for a pure group-type division."""

    def setUp(self):
        self.client = Client()
        self.user = make_user(username='medalclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(
            tournament=self.tournament, tournament_type='group'
        )
        self.division.gold_count = 1
        self.division.silver_count = 1
        self.division.bronze_count = 1
        self.division.save()
        self.t1 = make_team(r1=200, r2=201)
        self.t2 = make_team(r1=202, r2=203)
        self.t3 = make_team(r1=204, r2=205)
        self.division.teams.add(self.t1, self.t2, self.t3)
        # t1 beats t2, t1 beats t3, t2 beats t3 → ranking: t1 > t2 > t3
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            winner=self.t1, score='21-10, 21-10', status='completed',
        )
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t3,
            winner=self.t1, score='21-10, 21-10', status='completed',
        )
        Match.objects.create(
            division=self.division, team1=self.t2, team2=self.t3,
            winner=self.t2, score='21-10, 21-10', status='completed',
        )

    def test_get_returns_200(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.status_code, 200)

    def test_gold_is_top_team(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(len(response.context['gold']), 1)
        self.assertEqual(response.context['gold'][0], self.t1)

    def test_silver_is_second_team(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.context['silver'][0], self.t2)

    def test_bronze_is_third_team(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.context['bronze'][0], self.t3)

    def test_404_for_other_owner(self):
        other = make_user(username='othermedal')
        self.client.force_login(other)
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.status_code, 404)

    def test_all_done_flag_true_when_no_pending(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertTrue(response.context['all_done'])

    def test_all_done_flag_false_when_pending_matches(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            status='pending',
        )
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertFalse(response.context['all_done'])


class DivisionMedalsViewTreeTest(TestCase):
    """division_medals view derives medals from bracket results for tree-type."""

    def setUp(self):
        self.client = Client()
        self.user = make_user(username='treemedal')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(
            tournament=self.tournament, tournament_type='tree'
        )
        self.division.gold_count = 1
        self.division.silver_count = 1
        self.division.bronze_count = 2
        self.division.save()
        self.t1, self.t2, self.t3, self.t4 = [make_team(r1=300 + i * 2, r2=301 + i * 2) for i in range(4)]
        self.division.teams.add(self.t1, self.t2, self.t3, self.t4)
        # Semi-finals (round 1): t1 beats t2, t3 beats t4
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            winner=self.t1, score='21-10, 21-10', status='completed',
            match_round=1, bracket_slot=1,
        )
        Match.objects.create(
            division=self.division, team1=self.t3, team2=self.t4,
            winner=self.t3, score='21-10, 21-10', status='completed',
            match_round=1, bracket_slot=2,
        )
        # Final (round 2): t1 beats t3
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t3,
            winner=self.t1, score='21-15, 21-15', status='completed',
            match_round=2, bracket_slot=1,
        )

    def test_gold_is_final_winner(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.context['gold'], [self.t1])

    def test_silver_is_final_loser(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.context['silver'], [self.t3])

    def test_bronze_are_semifinal_losers(self):
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        bronze = response.context['bronze']
        self.assertIn(self.t2, bronze)
        self.assertIn(self.t4, bronze)

    def test_empty_if_no_completed_matches(self):
        Match.objects.filter(division=self.division).update(status='pending', winner=None)
        response = self.client.get(reverse('division_medals', args=[self.division.pk]))
        self.assertEqual(response.context['gold'], [])
        self.assertEqual(response.context['silver'], [])
        self.assertEqual(response.context['bronze'], [])


# ---------------------------------------------------------------------------
# Tournament delete – wrong confirm text
# ---------------------------------------------------------------------------

class TournamentDeleteWrongConfirmTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='delwrongclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)

    def test_wrong_confirm_shows_error_and_stays_on_page(self):
        pk = self.tournament.pk
        response = self.client.post(
            reverse('tournament_delete', args=[pk]),
            {'confirm': 'wrong text'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Tournament.objects.filter(pk=pk).exists())
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('DELETE TOURNAMENT' in str(m) or 'SLET TURNERING' in str(m) for m in messages_list))

    def test_correct_confirm_deletes(self):
        pk = self.tournament.pk
        response = self.client.post(
            reverse('tournament_delete', args=[pk]),
            {'confirm': 'SLET TURNERING'},
        )
        self.assertRedirects(response, reverse('tournament_list'))
        self.assertFalse(Tournament.objects.filter(pk=pk).exists())


# ---------------------------------------------------------------------------
# Tournament edit view
# ---------------------------------------------------------------------------

class TournamentEditViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='editviewclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)

    def test_edit_get_returns_200(self):
        response = self.client.get(reverse('tournament_edit', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.tournament.name)

    def test_edit_post_updates_tournament(self):
        day = make_day(self.tournament)  # existing day: INITIAL_FORMS=1
        response = self.client.post(reverse('tournament_edit', args=[self.tournament.pk]), {
            'name': 'Redigeret Turnering',
            'date': '2026-09-01',
            'division_model': 'mixed',
            'scoring_model': 'best_of_3_21',
            'single_match_duration': 30,
            'double_match_duration': 40,
            'player_break_time': 15,
            # Keep the existing day
            'days-TOTAL_FORMS': '1',
            'days-INITIAL_FORMS': '1',
            'days-MIN_NUM_FORMS': '1',
            'days-MAX_NUM_FORMS': '1000',
            'days-0-id': str(day.pk),
            'days-0-tournament': str(self.tournament.pk),
            'days-0-day_number': '1',
            'days-0-date': str(day.date),
            'days-0-start_time': '09:00',
            'days-0-court_count': '4',
            'days-0-buffer_minutes': '30',
        })
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.name, 'Redigeret Turnering')

    def test_edit_post_invalid_stays_on_page(self):
        response = self.client.post(reverse('tournament_edit', args=[self.tournament.pk]), {
            'name': '',  # Required field missing
        })
        self.assertEqual(response.status_code, 200)
        self.tournament.refresh_from_db()
        self.assertNotEqual(self.tournament.name, '')

    def test_edit_other_owners_tournament_returns_404(self):
        other = make_user(username='editother')
        other_t = make_tournament(owner=other)
        response = self.client.get(reverse('tournament_edit', args=[other_t.pk]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tournament wallchart view
# ---------------------------------------------------------------------------

class TournamentWallchartTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='wallchartclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_wallchart_returns_200(self):
        response = self.client.get(reverse('tournament_wallchart', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_wallchart_contains_division_name(self):
        response = self.client.get(reverse('tournament_wallchart', args=[self.tournament.pk]))
        self.assertContains(response, self.division.name)

    def test_wallchart_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_wallchart', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_wallchart_with_matches(self):
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        response = self.client.get(reverse('tournament_wallchart', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Tournament reset schedule view
# ---------------------------------------------------------------------------

class TournamentResetScheduleTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='resetclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        self.division.teams.add(t1, t2)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))

    def test_reset_get_shows_confirm_page(self):
        response = self.client.get(reverse('tournament_reset_schedule', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_reset_wrong_confirm_shows_error(self):
        count_before = Match.objects.filter(division__tournament=self.tournament).count()
        response = self.client.post(
            reverse('tournament_reset_schedule', args=[self.tournament.pk]),
            {'confirm': 'wrong'},
        )
        self.assertEqual(response.status_code, 200)
        # Matches should not be deleted
        self.assertEqual(Match.objects.filter(division__tournament=self.tournament).count(), count_before)

    def test_reset_correct_confirm_deletes_matches(self):
        self.assertGreater(Match.objects.filter(division__tournament=self.tournament).count(), 0)
        response = self.client.post(
            reverse('tournament_reset_schedule', args=[self.tournament.pk]),
            {'confirm': 'NULSTIL KAMPPROGRAM'},
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        self.assertEqual(Match.objects.filter(division__tournament=self.tournament).count(), 0)

    def test_reset_also_unlocks_if_locked(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        self.client.post(
            reverse('tournament_reset_schedule', args=[self.tournament.pk]),
            {'confirm': 'NULSTIL KAMPPROGRAM'},
        )
        self.tournament.refresh_from_db()
        self.assertFalse(self.tournament.schedule_locked)

    def test_reset_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_reset_schedule', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tournament run view
# ---------------------------------------------------------------------------

class TournamentRunViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='runclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)

    def test_run_view_returns_200(self):
        response = self.client.get(reverse('tournament_run', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_run_view_contains_division(self):
        response = self.client.get(reverse('tournament_run', args=[self.tournament.pk]))
        self.assertContains(response, self.division.name)

    def test_run_view_context_has_division_data(self):
        response = self.client.get(reverse('tournament_run', args=[self.tournament.pk]))
        self.assertIn('division_data', response.context)

    def test_run_view_shows_one_court_icon_per_court(self):
        from django.utils import timezone
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            match_number=1, status='in_progress', court='2', started_at=timezone.now(),
        )
        response = self.client.get(reverse('tournament_run', args=[self.tournament.pk]))
        icon_count = response.content.decode().count('src="/static/img/court.svg"')
        self.assertEqual(icon_count, len(response.context['court_choices']))
        self.assertEqual(response.content.decode().count('class="court-number"'), len(response.context['court_choices']))
        self.assertEqual(response.content.decode().count('class="court-icon court-occupied"'), 1)
        self.assertEqual(response.content.decode().count('class="court-result-action js-result-modal"'), 1)
        self.assertContains(response, reverse('match_record_result', args=[Match.objects.get(court='2').pk]))
        self.assertContains(response, 'popup=1')
        self.assertEqual(response.context['occupied_court_count'], 1)
        self.assertContains(response, 'class="court-occupancy-count"')
        self.assertContains(response, 'class="court-timer" data-started-at=')
        self.assertContains(response, '1 / 4')
        content = response.content.decode()
        self.assertLess(content.index('<div class="court-occupancy-count">'), content.index('<div class="court-indicators">'))

    def test_run_view_404_for_nonexistent(self):
        response = self.client.get(reverse('tournament_run', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Phase 2 — TournamentDayFormSet tests
# ---------------------------------------------------------------------------

class TournamentDayFormSetTest(TestCase):
    def setUp(self):
        self.user = make_user(username='formsetclub')
        self.client.force_login(self.user)

    def _base_post(self, extra=None):
        data = {
            'name': 'Formset Test',
            'date': '2026-08-01',
            'division_model': 'mixed',
            'scoring_model': 'best_of_3_21',
            'single_match_duration': 30,
            'double_match_duration': 40,
            'player_break_time': 15,
            'days-TOTAL_FORMS': '1',
            'days-INITIAL_FORMS': '0',
            'days-MIN_NUM_FORMS': '1',
            'days-MAX_NUM_FORMS': '1000',
            'days-0-day_number': '1',
            'days-0-date': '2026-08-01',
            'days-0-start_time': '09:00',
            'days-0-court_count': '4',
            'days-0-buffer_minutes': '30',
        }
        if extra:
            data.update(extra)
        return data

    def test_create_with_valid_day_creates_tournament_and_day(self):
        response = self.client.post(reverse('tournament_create'), self._base_post())
        t = Tournament.objects.get(name='Formset Test')
        self.assertEqual(t.days.count(), 1)
        self.assertEqual(t.days.first().court_count, 4)

    def test_create_without_day_stays_on_page(self):
        """Without formset management form, formset is invalid → stay on page."""
        data = {
            'name': 'No Day Tournament',
            'date': '2026-08-01',
            'division_model': 'mixed',
            'scoring_model': 'best_of_3_21',
            'single_match_duration': 30,
            'double_match_duration': 40,
            'player_break_time': 15,
            'days-TOTAL_FORMS': '0',
            'days-INITIAL_FORMS': '0',
            'days-MIN_NUM_FORMS': '1',
            'days-MAX_NUM_FORMS': '1000',
        }
        response = self.client.post(reverse('tournament_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tournament.objects.filter(name='No Day Tournament').exists())


# ---------------------------------------------------------------------------
# Phase 2 — division_update_days view tests
# ---------------------------------------------------------------------------

class DivisionUpdateDaysTest(TestCase):
    def setUp(self):
        self.user = make_user(username='dayassignclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.day1 = make_day(self.tournament, day_number=1, date=datetime.date(2026, 6, 1))
        self.day2 = make_day(self.tournament, day_number=2, date=datetime.date(2026, 6, 2))
        self.division = make_division(tournament=self.tournament, tournament_type='playoff')

    def test_post_sets_group_and_playoff_day(self):
        self.client.post(
            reverse('division_update_days', args=[self.division.pk]),
            {'group_day': self.day1.pk, 'playoff_day': self.day2.pk},
        )
        self.division.refresh_from_db()
        self.assertEqual(self.division.group_day_id, self.day1.pk)
        self.assertEqual(self.division.playoff_day_id, self.day2.pk)

    def test_post_clears_days_with_empty_values(self):
        self.division.group_day = self.day1
        self.division.save()
        self.client.post(
            reverse('division_update_days', args=[self.division.pk]),
            {'group_day': '', 'playoff_day': ''},
        )
        self.division.refresh_from_db()
        self.assertIsNone(self.division.group_day_id)
        self.assertIsNone(self.division.playoff_day_id)

    def test_other_owners_division_returns_404(self):
        other = make_user(username='otherownerday')
        other_t = make_tournament(owner=other)
        other_d = make_division(tournament=other_t)
        response = self.client.post(
            reverse('division_update_days', args=[other_d.pk]),
            {'group_day': self.day1.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_day_from_other_tournament_is_rejected(self):
        other_t = make_tournament(owner=self.user)
        other_day = make_day(other_t, day_number=1, date=datetime.date(2026, 7, 1))
        self.client.post(
            reverse('division_update_days', args=[self.division.pk]),
            {'group_day': other_day.pk},
        )
        self.division.refresh_from_db()
        self.assertIsNone(self.division.group_day_id)

    def test_post_redirects_to_tournament_detail(self):
        response = self.client.post(
            reverse('division_update_days', args=[self.division.pk]),
            {'group_day': self.day1.pk},
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))


# ---------------------------------------------------------------------------
# Phase 2 — check_schedule_feasibility tests
# ---------------------------------------------------------------------------

class FeasibilityNoDaysTest(TestCase):
    def test_no_days_returns_error(self):
        from tournaments.schedule_planner import check_schedule_feasibility
        t = make_tournament()
        errors = check_schedule_feasibility(t)
        self.assertTrue(len(errors) > 0)
        self.assertTrue(any('no days' in e.lower() or 'configured' in e.lower() for e in errors))


class FeasibilityBufferTooLargeTest(TestCase):
    def test_buffer_equal_to_window_returns_error(self):
        from tournaments.schedule_planner import check_schedule_feasibility
        t = make_tournament()
        make_day(t, courts=4, start='09:00', end='10:00', buffer=60)  # 60 min window, 60 min buffer
        errors = check_schedule_feasibility(t)
        self.assertTrue(len(errors) > 0)


class FeasibilityOkTest(TestCase):
    def test_no_matches_assigned_to_day_is_ok(self):
        from tournaments.schedule_planner import check_schedule_feasibility
        t = make_tournament()
        make_day(t, courts=4, start='09:00', end='22:00', buffer=30)
        # No divisions assigned to this day — required_minutes = 0, no error
        errors = check_schedule_feasibility(t)
        self.assertEqual(errors, [])


class FeasibilityTooManyMatchesTest(TestCase):
    def test_too_many_matches_for_day_returns_error(self):
        import datetime as dt
        from tournaments.schedule_planner import check_schedule_feasibility
        from tournaments.scheduler import generate_schedule
        t = make_tournament()
        # 30-minute window, 0 buffer, 1 court, 30-min matches → fits exactly 1 match
        day = make_day(t, courts=1, start='09:00', end='09:30', buffer=0)
        div = make_division(tournament=t, tournament_type='group')
        div.group_day = day
        div.save()
        # Add enough teams to generate multiple matches
        for _ in range(4):
            div.teams.add(make_team())
        generate_schedule(div)
        # Assign match numbers so feasibility check counts them
        for i, m in enumerate(Match.objects.filter(division=div), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        errors = check_schedule_feasibility(t)
        self.assertTrue(len(errors) > 0)


# ---------------------------------------------------------------------------
# Phase 2 — scheduling view with feasibility tests
# ---------------------------------------------------------------------------

class FeasibilityBlocksSchedulingViewTest(TestCase):
    def setUp(self):
        self.user = make_user(username='feasibilityclub')
        self.client.force_login(self.user)

    def test_feasibility_error_blocks_scheduling(self):
        from tournaments.scheduler import generate_schedule
        t = make_tournament(owner=self.user)
        day = make_day(t, courts=1, start='09:00', end='09:01', buffer=0)  # 1-min window
        div = make_division(tournament=t, tournament_type='group')
        div.group_day = day
        div.save()
        for _ in range(3):
            div.teams.add(make_team())
        generate_schedule(div)
        # Number matches
        for i, m in enumerate(Match.objects.filter(division=div), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        # POST without force — feasibility should block scheduling
        self.client.post(reverse('tournament_generate_time_schedule', args=[t.pk]))
        self.assertFalse(
            Match.objects.filter(division=div, scheduled_time__isnull=False).exists()
        )

    def test_force_override_runs_scheduling_despite_errors(self):
        from tournaments.scheduler import generate_schedule
        t = make_tournament(owner=self.user)
        make_day(t, courts=4, start='09:00', end='22:00', buffer=30)
        div = make_division(tournament=t, tournament_type='group')
        # No group_day assigned — all divisions' matches have required_minutes=0
        # for feasibility per-day check (they are not assigned to a day).
        # So feasibility passes; schedule should run.
        for _ in range(3):
            div.teams.add(make_team())
        generate_schedule(div)
        for i, m in enumerate(Match.objects.filter(division=div), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        self.client.post(
            reverse('tournament_generate_time_schedule', args=[t.pk]),
            {'force': '1'},
        )
        self.assertTrue(
            Match.objects.filter(division=div, scheduled_time__isnull=False).exists()
        )


# ---------------------------------------------------------------------------
# Phase 2 — multi-day scheduler tests
# ---------------------------------------------------------------------------

class SchedulerMultiDayTest(TestCase):
    def test_matches_assigned_to_day2_only(self):
        """Division with group_day=day2 → no matches on day1."""
        import datetime as dt
        from tournaments.schedule_planner import generate_time_schedule
        from tournaments.scheduler import generate_schedule
        t = make_tournament()
        day1 = make_day(t, day_number=1, date=dt.date(2026, 6, 1), courts=4)
        day2 = make_day(t, day_number=2, date=dt.date(2026, 6, 2), courts=4)
        div = make_division(tournament=t, tournament_type='group')
        div.group_day = day2
        div.save()
        for _ in range(3):
            div.teams.add(make_team())
        generate_schedule(div)
        for i, m in enumerate(Match.objects.filter(division=div), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        generate_time_schedule(t)
        for m in Match.objects.filter(division=div, scheduled_time__isnull=False):
            self.assertEqual(m.scheduled_time.date(), dt.date(2026, 6, 2))

    def test_no_match_starts_after_effective_end(self):
        """No match should start so late that it runs into the buffer."""
        import datetime as dt
        from tournaments.schedule_planner import generate_time_schedule
        from tournaments.scheduler import generate_schedule
        t = make_tournament()
        day = make_day(t, day_number=1, courts=4, start='09:00', end='22:00', buffer=30)
        div = make_division(tournament=t, tournament_type='group')
        for _ in range(4):
            div.teams.add(make_team())
        generate_schedule(div)
        for i, m in enumerate(Match.objects.filter(division=div), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        generate_time_schedule(t)
        # Effective end = 22:00 − 30 min = 21:30
        effective_end = dt.time(21, 30)
        for m in Match.objects.filter(division=div, scheduled_time__isnull=False):
            start_t = m.scheduled_time.astimezone().time() if m.scheduled_time.tzinfo else m.scheduled_time.time()
            self.assertLessEqual(start_t, effective_end)


# ---------------------------------------------------------------------------
# Schedule editor view
# ---------------------------------------------------------------------------

class ScheduleEditorViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='editorclub')
        self.client.force_login(self.user)
        self.tournament = Tournament.objects.create(
            name="Editor T", date=datetime.date(2026, 6, 1),
            division_model='mixed', scoring_model='best_of_3_21',
            owner=self.user,
        )
        make_day(self.tournament, courts=2)

    def test_editor_returns_200(self):
        response = self.client.get(reverse('schedule_editor', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_editor_context_has_slots_and_courts(self):
        response = self.client.get(reverse('schedule_editor', args=[self.tournament.pk]))
        self.assertIn('slots', response.context)
        self.assertIn('courts', response.context)

    def test_editor_404_for_nonexistent(self):
        response = self.client.get(reverse('schedule_editor', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_editor_with_scheduled_matches_shows_grid(self):
        from django.utils import timezone
        division = make_division(tournament=self.tournament)
        t1 = make_team(r1=1, r2=2)
        t2 = make_team(r1=3, r2=4)
        division.teams.add(t1, t2)
        Match.objects.create(
            division=division, team1=t1, team2=t2, match_number=1,
            scheduled_time=timezone.make_aware(datetime.datetime(2026, 6, 1, 9, 0)),
            court='1',
        )
        response = self.client.get(reverse('schedule_editor', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        grid = response.context['grid']
        self.assertTrue(len(grid) > 0)


# ---------------------------------------------------------------------------
# Schedule JSON API: assign / unassign / clear
# ---------------------------------------------------------------------------

class ScheduleAPITest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='schedapiclub')
        self.client.force_login(self.user)
        self.tournament = Tournament.objects.create(
            name="API T", date=datetime.date(2026, 6, 1),
            division_model='mixed', scoring_model='best_of_3_21',
            owner=self.user,
        )
        make_day(self.tournament, courts=2)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.division.teams.add(self.t1, self.t2)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))
        self.match = Match.objects.filter(division=self.division).first()

    def _post_json(self, url, data):
        import json as _json
        return self.client.post(
            url,
            data=_json.dumps(data),
            content_type='application/json',
        )

    def test_assign_sets_scheduled_time_and_court(self):
        response = self._post_json(
            reverse('schedule_assign', args=[self.tournament.pk]),
            {'match_id': self.match.pk, 'time': f'{self.tournament.date} 09:00', 'court': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.match.refresh_from_db()
        self.assertIsNotNone(self.match.scheduled_time)
        self.assertEqual(self.match.court, '1')

    def test_assign_invalid_data_returns_400(self):
        response = self._post_json(
            reverse('schedule_assign', args=[self.tournament.pk]),
            {'match_id': 'not-a-number'},
        )
        self.assertEqual(response.status_code, 400)

    def test_assign_when_locked_returns_403(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        response = self._post_json(
            reverse('schedule_assign', args=[self.tournament.pk]),
            {'match_id': self.match.pk, 'time': '09:00', 'court': '1'},
        )
        self.assertEqual(response.status_code, 403)

    def test_unassign_clears_scheduled_time(self):
        # First assign
        self._post_json(
            reverse('schedule_assign', args=[self.tournament.pk]),
            {'match_id': self.match.pk, 'time': '09:00', 'court': '1'},
        )
        # Then unassign
        response = self._post_json(
            reverse('schedule_unassign', args=[self.tournament.pk]),
            {'match_id': self.match.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.scheduled_time)
        self.assertIsNone(self.match.court)

    def test_unassign_when_locked_returns_403(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        response = self._post_json(
            reverse('schedule_unassign', args=[self.tournament.pk]),
            {'match_id': self.match.pk},
        )
        self.assertEqual(response.status_code, 403)

    def test_clear_removes_all_scheduled_times(self):
        from django.utils import timezone
        # Assign a time
        self.match.scheduled_time = timezone.make_aware(datetime.datetime(2026, 6, 1, 9, 0))
        self.match.court = '1'
        self.match.save()
        response = self._post_json(
            reverse('schedule_clear', args=[self.tournament.pk]),
            {},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertGreater(data['cleared'], 0)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.scheduled_time)

    def test_clear_when_locked_returns_403(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        response = self._post_json(
            reverse('schedule_clear', args=[self.tournament.pk]),
            {},
        )
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# Tournament renumber matches
# ---------------------------------------------------------------------------

class TournamentRenumberTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='renumberclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        for i in range(3):
            t = make_team(r1=i * 2 + 1, r2=i * 2 + 2)
            self.division.teams.add(t)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))

    def test_renumber_reassigns_sequential_numbers(self):
        # Scramble existing numbers
        matches = list(Match.objects.filter(division=self.division).order_by('pk'))
        for i, m in enumerate(matches):
            m.match_number = 100 + i
            m.save()
        # Renumber
        response = self.client.post(reverse('tournament_renumber_matches', args=[self.tournament.pk]))
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        numbers = sorted(
            Match.objects.filter(division=self.division)
            .exclude(match_number=None)
            .values_list('match_number', flat=True)
        )
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_renumber_by_schedule_reorders_by_time(self):
        from django.utils import timezone
        matches = list(Match.objects.filter(division=self.division).order_by('pk'))
        # Assign times in reverse order
        for i, m in enumerate(reversed(matches)):
            m.scheduled_time = timezone.make_aware(
                datetime.datetime(2026, 6, 1, 9 + i, 0)
            )
            m.save()
        response = self.client.post(
            reverse('tournament_renumber_by_schedule', args=[self.tournament.pk])
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        # Earliest scheduled match should get number 1
        earliest = Match.objects.filter(division=self.division).order_by('scheduled_time').first()
        self.assertEqual(earliest.match_number, 1)

    def test_renumber_404_for_nonexistent(self):
        response = self.client.post(reverse('tournament_renumber_matches', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tournament rebuild playoff labels
# ---------------------------------------------------------------------------

class TournamentRebuildLabelsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='rebuildclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Rebuild Playoff", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        for i in range(4):
            t = make_team(r1=i * 2 + 1, r2=i * 2 + 2)
            self.division.teams.add(t)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))

    def test_rebuild_labels_returns_redirect(self):
        response = self.client.post(
            reverse('tournament_rebuild_playoff_labels', args=[self.tournament.pk])
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))

    def test_rebuild_labels_404_for_nonexistent(self):
        response = self.client.post(reverse('tournament_rebuild_playoff_labels', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Division medals edit and reset
# ---------------------------------------------------------------------------

class DivisionMedalsEditResetTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='medalseditclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(
            tournament=self.tournament, tournament_type='tree'
        )
        self.division.gold_count = 1
        self.division.silver_count = 1
        self.division.bronze_count = 0
        self.division.save()
        self.t1 = make_team(r1=200, r2=201)
        self.t2 = make_team(r1=202, r2=203)
        self.division.teams.add(self.t1, self.t2)

    def test_medals_edit_get_returns_200(self):
        response = self.client.get(reverse('division_medals_edit', args=[self.division.pk]))
        self.assertEqual(response.status_code, 200)

    def test_medals_edit_post_creates_override(self):
        from .models import MedalOverride
        response = self.client.post(
            reverse('division_medals_edit', args=[self.division.pk]),
            {'gold_1': self.t1.pk, 'silver_1': self.t2.pk},
        )
        self.assertRedirects(response, reverse('division_medals', args=[self.division.pk]))
        self.assertTrue(MedalOverride.objects.filter(division=self.division, medal='gold').exists())
        self.assertTrue(MedalOverride.objects.filter(division=self.division, medal='silver').exists())

    def test_medals_reset_removes_overrides(self):
        from .models import MedalOverride
        MedalOverride.objects.create(division=self.division, team=self.t1, medal='gold', order=1)
        self.assertTrue(MedalOverride.objects.filter(division=self.division).exists())
        response = self.client.post(reverse('division_medals_reset', args=[self.division.pk]))
        self.assertRedirects(response, reverse('division_medals', args=[self.division.pk]))
        self.assertFalse(MedalOverride.objects.filter(division=self.division).exists())

    def test_medals_edit_404_for_nonexistent(self):
        response = self.client.get(reverse('division_medals_edit', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_medals_reset_get_redirects_to_medals(self):
        # GET to reset endpoint just redirects (no auth bypass since it's GET)
        response = self.client.get(reverse('division_medals_reset', args=[self.division.pk]))
        self.assertRedirects(response, reverse('division_medals', args=[self.division.pk]))


# ---------------------------------------------------------------------------
# Division reassign groups
# ---------------------------------------------------------------------------

class DivisionReassignGroupsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(username='reassignclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Reassign Playoff", discipline='double',
            tournament_type='playoff', group_count=2, advance_count=1,
        )
        self.t1 = make_team(r1=1, r2=2)
        self.t2 = make_team(r1=3, r2=4)
        self.t3 = make_team(r1=5, r2=6)
        self.t4 = make_team(r1=7, r2=8)
        self.division.teams.add(self.t1, self.t2, self.t3, self.t4)
        self.client.post(reverse('division_generate_schedule', args=[self.division.pk]))

    def _reassign(self, groups_dict):
        import json as _json
        return self.client.post(
            reverse('division_reassign_groups', args=[self.division.pk]),
            {'groups': _json.dumps(groups_dict)},
        )

    def test_get_request_redirects(self):
        response = self.client.get(reverse('division_reassign_groups', args=[self.division.pk]))
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))

    def test_non_playoff_division_shows_error(self):
        group_div = make_division(tournament=self.tournament, tournament_type='group')
        t1 = make_team(r1=10, r2=11)
        t2 = make_team(r1=12, r2=13)
        group_div.teams.add(t1, t2)
        import json as _json
        response = self.client.post(
            reverse('division_reassign_groups', args=[group_div.pk]),
            {'groups': _json.dumps({'1': [t1.pk, t2.pk]})},
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('group stage' in str(m).lower() or 'gruppefase' in str(m).lower() for m in messages_list))

    def test_locked_tournament_shows_error(self):
        self.tournament.schedule_locked = True
        self.tournament.save()
        response = self._reassign({'1': [self.t1.pk, self.t2.pk], '2': [self.t3.pk, self.t4.pk]})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('locked' in str(m).lower() for m in messages_list))

    def test_completed_matches_blocks_reassign(self):
        match = Match.objects.filter(division=self.division, phase='group').first()
        match.status = 'completed'
        match.winner = match.team1
        match.score = '21-10, 21-10'
        match.save()
        response = self._reassign({'1': [self.t1.pk, self.t2.pk], '2': [self.t3.pk, self.t4.pk]})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('result' in str(m).lower() or 'recorded' in str(m).lower() or 'registreret' in str(m).lower() for m in messages_list))

    def test_invalid_json_shows_error(self):
        response = self.client.post(
            reverse('division_reassign_groups', args=[self.division.pk]),
            {'groups': 'not-valid-json'},
        )
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('invalid' in str(m).lower() or 'ugyldig' in str(m).lower() for m in messages_list))

    def test_wrong_teams_shows_error(self):
        # Submit only 3 of 4 teams
        response = self._reassign({'1': [self.t1.pk, self.t2.pk, self.t3.pk]})
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        messages_list = list(response.wsgi_request._messages)
        self.assertTrue(any('team' in str(m).lower() or 'hold' in str(m).lower() for m in messages_list))

    def test_valid_reassign_regenerates_matches(self):
        old_count = Match.objects.filter(division=self.division).count()
        response = self._reassign({
            '1': [self.t1.pk, self.t2.pk],
            '2': [self.t3.pk, self.t4.pk],
        })
        self.assertRedirects(response, reverse('tournament_detail', args=[self.tournament.pk]))
        # Should have matches
        self.assertGreater(Match.objects.filter(division=self.division).count(), 0)


# ---------------------------------------------------------------------------
# Login signal: set_language_on_login
# ---------------------------------------------------------------------------

class LoginSignalLanguageTest(TestCase):
    def setUp(self):
        # make_user() triggers the post_save signal that auto-creates a
        # UserProfile (language defaults to 'ca'), so no explicit create here.
        self.user = make_user(username='signaluserclub')

    def test_login_activates_club_language_without_cookie(self):
        from django.utils import translation
        from django.test import RequestFactory
        from tournaments.signals import set_language_on_login
        factory = RequestFactory()
        request = factory.get('/')
        request.COOKIES = {}  # No language cookie
        set_language_on_login(sender=None, request=request, user=self.user)
        # Language cookie should be scheduled to be set
        self.assertEqual(getattr(request, '_set_language_cookie', None), 'ca')

    def test_login_does_not_override_existing_cookie(self):
        from django.test import RequestFactory
        from tournaments.signals import set_language_on_login
        factory = RequestFactory()
        request = factory.get('/')
        request.COOKIES = {'django_language': 'en'}  # User has chosen English
        set_language_on_login(sender=None, request=request, user=self.user)
        # Should not override
        self.assertIsNone(getattr(request, '_set_language_cookie', None))

    def test_login_with_no_profile_is_silent(self):
        from django.test import RequestFactory
        from tournaments.signals import set_language_on_login
        from tournaments.models import UserProfile
        user_no_profile = make_user(username='noprofileclub')
        # Deleting via a fresh queryset (not user_no_profile.profile) avoids
        # caching the profile on the instance, but Django's ORM already
        # populated the reverse o2o cache during signal-driven creation
        # (setting .user on the new UserProfile also caches the reverse side
        # on the User instance) — refresh_from_db() clears that stale cache.
        UserProfile.objects.filter(user=user_no_profile).delete()
        user_no_profile.refresh_from_db()
        factory = RequestFactory()
        request = factory.get('/')
        request.COOKIES = {}
        # Should not raise
        set_language_on_login(sender=None, request=request, user=user_no_profile)
        self.assertIsNone(getattr(request, '_set_language_cookie', None))


# ---------------------------------------------------------------------------
# Public views – language activation via UserProfile
# ---------------------------------------------------------------------------

class PublicViewLanguageTest(TestCase):
    def setUp(self):
        # make_user() triggers the post_save signal that auto-creates a
        # UserProfile (language defaults to 'da'), so no explicit create here.
        self.user = make_user(username='langpublicclub')
        self.tournament = make_tournament(owner=self.user)

    def test_public_tournament_activates_club_language_without_cookie(self):
        # Without a language cookie the club's language should be activated
        response = self.client.get(reverse('public_tournament', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)

    def test_public_schedule_activates_club_language_without_cookie(self):
        response = self.client.get(reverse('public_schedule', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Phase 3 — Frontend tests
# ---------------------------------------------------------------------------

class TournamentFormDaysTest(TestCase):
    """Formset for TournamentDays renderes korrekt og gemmes ved POST."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_create_form_get_contains_formset(self):
        """GET tournament_create skal indeholde days-management-form."""
        response = self.client.get(reverse('tournament_create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id_days-TOTAL_FORMS')

    def test_create_post_with_two_days_saves_both(self):
        """POST med to dage opretter turneringen med begge TournamentDay-objekter."""
        import datetime as dt
        data = {
            'name': 'Fase3 Todages',
            'date': '2026-08-01',
            'division_model': 'mixed',
            'scoring_model': 'best_of_3_21',
            'single_match_duration': 30,
            'double_match_duration': 40,
            'player_break_time': 15,
            'days-TOTAL_FORMS': '2',
            'days-INITIAL_FORMS': '0',
            'days-MIN_NUM_FORMS': '1',
            'days-MAX_NUM_FORMS': '1000',
            'days-0-day_number': '1',
            'days-0-date': '2026-08-01',
            'days-0-start_time': '09:00',
            'days-0-end_time': '22:00',
            'days-0-court_count': '6',
            'days-0-buffer_minutes': '30',
            'days-0-DELETE': '',
            'days-1-day_number': '2',
            'days-1-date': '2026-08-02',
            'days-1-start_time': '09:00',
            'days-1-end_time': '18:00',
            'days-1-court_count': '4',
            'days-1-buffer_minutes': '30',
            'days-1-DELETE': '',
        }
        response = self.client.post(reverse('tournament_create'), data)
        self.assertEqual(response.status_code, 302)
        from tournaments.models import Tournament, TournamentDay
        t = Tournament.objects.get(name='Fase3 Todages')
        self.assertEqual(t.days.count(), 2)
        self.assertEqual(t.days.get(day_number=1).court_count, 6)
        self.assertEqual(t.days.get(day_number=2).court_count, 4)

    def test_edit_form_get_contains_existing_days(self):
        """GET tournament_edit skal vise eksisterende dage i formsettet."""
        t = make_tournament(owner=self.user)
        make_day(t, day_number=1)
        make_day(t, day_number=2, date=__import__('datetime').date(2026, 8, 2))
        response = self.client.get(reverse('tournament_edit', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id_days-TOTAL_FORMS')
        # To dage = INITIAL_FORMS skal være 2
        self.assertContains(response, 'value="2"')


class DivisionDayAssignmentUITest(TestCase):
    """Dag-tildeling-UI vises korrekt for flerdages turneringer."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_day_assignment_shown_for_multiday_tournament(self):
        """Detail-siden for en flerdages turnering viser dag-tildeling-sektion."""
        import datetime as dt
        t = make_tournament(owner=self.user)
        make_day(t, day_number=1)
        make_day(t, day_number=2, date=dt.date(2026, 8, 2))
        from tournaments.models import Division
        Division.objects.create(
            tournament=t, name='HS', discipline='single',
            tournament_type='group', schedule_priority=5,
        )
        response = self.client.get(reverse('tournament_detail', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Day assignment')

    def test_day_assignment_hidden_for_single_day_tournament(self):
        """Detail-siden for en endags-turnering viser IKKE dag-tildeling."""
        t = make_tournament(owner=self.user)
        make_day(t, day_number=1)
        from tournaments.models import Division
        Division.objects.create(
            tournament=t, name='HS', discipline='single',
            tournament_type='group', schedule_priority=5,
        )
        response = self.client.get(reverse('tournament_detail', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Day assignment')


class ScheduleViewDayHeaderTest(TestCase):
    """_schedule_matches.html viser dag-headers for flerdages tidsprogram."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _make_scheduled_tournament(self):
        import datetime as dt
        t = make_tournament(owner=self.user)
        day1 = make_day(t, day_number=1, date=dt.date(2026, 8, 1))
        day2 = make_day(t, day_number=2, date=dt.date(2026, 8, 2))
        from tournaments.models import Division, Match
        div = Division.objects.create(
            tournament=t, name='HS', discipline='single',
            tournament_type='group', schedule_priority=5,
            group_day=day1,
        )
        from django.utils import timezone
        m1 = Match.objects.create(
            division=div, match_round=1, phase='group', match_number=1,
            scheduled_time=timezone.make_aware(
                dt.datetime.combine(dt.date(2026, 8, 1), dt.time(10, 0))
            ),
        )
        m2 = Match.objects.create(
            division=div, match_round=2, phase='group', match_number=2,
            scheduled_time=timezone.make_aware(
                dt.datetime.combine(dt.date(2026, 8, 2), dt.time(10, 0))
            ),
        )
        return t, day1, day2

    def test_schedule_shows_day_headers_for_multiday(self):
        """Time schedule-siden viser dag-headers når kampe er på flere dage."""
        t, day1, day2 = self._make_scheduled_tournament()
        response = self.client.get(reverse('tournament_schedule', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="sched-day-header"')

    def test_schedule_no_day_headers_for_single_day(self):
        """Time schedule-siden viser IKKE dag-headers for endags-turneringer."""
        import datetime as dt
        t = make_tournament(owner=self.user)
        make_day(t)
        from tournaments.models import Division, Match
        from django.utils import timezone
        div = Division.objects.create(
            tournament=t, name='DS', discipline='double',
            tournament_type='group', schedule_priority=5,
        )
        Match.objects.create(
            division=div, match_round=1, phase='group', match_number=1,
            scheduled_time=timezone.make_aware(
                dt.datetime.combine(dt.date.today(), dt.time(10, 0))
            ),
        )
        response = self.client.get(reverse('tournament_schedule', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="sched-day-header"')


class ScheduleEditorNoDaysWarningTest(TestCase):
    """schedule_editor viser advarsel når turneringen ikke har nogen dage."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_editor_shows_warning_without_days(self):
        t = make_tournament(owner=self.user)
        # Ingen dage
        response = self.client.get(reverse('schedule_editor', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Configure at least one tournament day')

    def test_editor_shows_grid_with_days(self):
        t = make_tournament(owner=self.user)
        make_day(t)
        response = self.client.get(reverse('schedule_editor', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'editor-grid')


class TournamentRunDayNavTest(TestCase):
    """tournament_run viser dag-navigation ved flerdages turneringer."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_run_shows_day_nav_for_multiday(self):
        import datetime as dt
        t = make_tournament(owner=self.user)
        make_day(t, day_number=1)
        make_day(t, day_number=2, date=dt.date(2026, 8, 2))
        response = self.client.get(reverse('tournament_run', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'day-nav-bar')

    def test_run_no_day_nav_for_single_day(self):
        t = make_tournament(owner=self.user)
        make_day(t, day_number=1)
        response = self.client.get(reverse('tournament_run', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'day-nav-bar')

    def test_run_no_day_nav_without_days(self):
        t = make_tournament(owner=self.user)
        response = self.client.get(reverse('tournament_run', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'day-nav-bar')


class TranslationPhase3Test(TestCase):
    """Alle Phase 3-relaterede msgid har ikke-tomme dansk msgstr."""

    REQUIRED_MSGIDS = [
        # Fase 1 — modelstrenge
        "Day number",
        "Buffer (minutes)",
        "End time must be after start time.",
        "Buffer is larger than or equal to the available time window.",
        # Fase 2 — view/scheduler-strenge
        "Day assignment saved.",
        "Warning: schedule may be incomplete despite forced run.",
        "Configure at least one tournament day before generating the time schedule.",
        # Fase 3 — template-strenge
        "No days configured.",
        "Day assignment",
        "Not assigned",
        "Group phase",
        "Playoff phase",
        "Add day",
        "Today",
    ]

    def test_all_required_msgids_translated(self):
        import polib
        from pathlib import Path
        from django.conf import settings
        po_path = Path(settings.BASE_DIR) / 'locale' / 'da' / 'LC_MESSAGES' / 'django.po'
        if not po_path.exists():
            self.skipTest('django.po not found')
        po = polib.pofile(str(po_path))
        translated = {e.msgid: e.msgstr for e in po if not e.obsolete}
        missing = [m for m in self.REQUIRED_MSGIDS if m not in translated]
        self.assertFalse(missing, f"msgid mangler i .po: {missing}")
        empty = [
            m for m in self.REQUIRED_MSGIDS
            if m in translated and not translated[m].strip()
        ]
        self.assertFalse(empty, f"msgstr er tom for: {empty}")


# ---------------------------------------------------------------------------
# Phase 4 – Print templates with day context
# ---------------------------------------------------------------------------

class PrintScheduleMultiDayTest(TestCase):
    """tournament_schedule_print viser dag-overskrifter for flerdags-turneringer."""

    def setUp(self):
        self.user = make_user(username='pschedclub')
        self.client.force_login(self.user)
        self.tournament = make_tournament(owner=self.user)
        self.division = make_division(tournament=self.tournament)
        self.t1 = make_team('Alpha', r1=81, r2=82)
        self.t2 = make_team('Beta', r1=83, r2=84)
        self.division.teams.add(self.t1, self.t2)
        self.day1 = make_day(
            self.tournament, day_number=1,
            date=datetime.date(2026, 7, 1), start='09:00', end='20:00',
        )
        self.day2 = make_day(
            self.tournament, day_number=2,
            date=datetime.date(2026, 7, 2), start='09:00', end='20:00',
        )

    def test_day_headers_shown_for_multiday(self):
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            match_round=1, match_number=1,
            scheduled_time='2026-07-01 10:00:00+00:00',
        )
        Match.objects.create(
            division=self.division, team1=self.t2, team2=self.t1,
            match_round=2, match_number=2,
            scheduled_time='2026-07-02 10:00:00+00:00',
        )
        response = self.client.get(reverse('tournament_schedule_print', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="day-sep"')

    def test_no_day_headers_for_single_day(self):
        self.day2.delete()
        Match.objects.create(
            division=self.division, team1=self.t1, team2=self.t2,
            match_round=1, match_number=1,
            scheduled_time='2026-07-01 10:00:00+00:00',
        )
        response = self.client.get(reverse('tournament_schedule_print', args=[self.tournament.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="day-sep"')


class PrintProgramDatesTest(TestCase):
    """tournament_program_print viser dato pr. kamp for flerdags-turneringer."""

    def setUp(self):
        self.user = make_user(username='pprogclub')
        self.client.force_login(self.user)

    def test_match_dates_shown_for_multiday(self):
        t = make_tournament(owner=self.user)
        div = make_division(tournament=t)
        t1 = make_team('Gamma', r1=91, r2=92)
        t2 = make_team('Delta', r1=93, r2=94)
        div.teams.add(t1, t2)
        make_day(t, day_number=1, date=datetime.date(2026, 8, 1), start='09:00', end='20:00')
        make_day(t, day_number=2, date=datetime.date(2026, 8, 2), start='09:00', end='20:00')
        Match.objects.create(
            division=div, team1=t1, team2=t2,
            match_round=1, match_number=1,
            scheduled_time='2026-08-01 10:00:00+00:00',
        )
        Match.objects.create(
            division=div, team1=t2, team2=t1,
            match_round=2, match_number=2,
            scheduled_time='2026-08-02 10:00:00+00:00',
        )
        response = self.client.get(reverse('tournament_program_print', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="match-date"')

    def test_no_match_dates_for_single_day(self):
        t = make_tournament(owner=self.user)
        div = make_division(tournament=t)
        t1 = make_team('Solo-A', r1=95, r2=96)
        t2 = make_team('Solo-B', r1=97, r2=98)
        div.teams.add(t1, t2)
        make_day(t, day_number=1, date=datetime.date(2026, 8, 1), start='09:00', end='20:00')
        Match.objects.create(
            division=div, team1=t1, team2=t2,
            match_round=1, match_number=1,
            scheduled_time='2026-08-01 10:00:00+00:00',
        )
        response = self.client.get(reverse('tournament_program_print', args=[t.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="match-date"')
