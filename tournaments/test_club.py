"""Tests for the club-specific adaptations (3x15 scoring, multi-group leagues, Catalan, etc.)."""
import datetime

from django.test import TestCase

from .forms import MatchResultForm, _validate_set
from .models import Match, Tournament
from .tests import make_division, make_team, make_user


class Scoring3x15Test(TestCase):
    """BWF 3x15: 2-point lead from 14-14, capped at 21 (20-20 → next point wins)."""

    def test_valid_sets(self):
        for a, b in [(15, 0), (15, 13), (16, 14), (20, 18), (21, 19), (21, 20), (13, 15)]:
            self.assertEqual(_validate_set(a, b, 15, 21), '', f"{a}-{b} should be valid")

    def test_invalid_sets(self):
        for a, b in [(15, 14), (22, 20), (17, 14), (14, 12), (21, 18), (30, 28)]:
            self.assertNotEqual(_validate_set(a, b, 15, 21), '', f"{a}-{b} should be invalid")

    def test_21_point_rules_unchanged(self):
        self.assertEqual(_validate_set(30, 29), '')
        self.assertEqual(_validate_set(22, 20), '')

    def _form(self, score, winner_is_t1=True):
        user = make_user(username='club3x15')
        tournament = Tournament.objects.create(
            name="3x15", date=datetime.date.today(), division_model='youth',
            scoring_model='best_of_5_15', owner=user,
        )
        division = make_division(tournament=tournament, discipline='single')
        t1, t2 = make_team(r1=1, r2=None), make_team(r1=2, r2=None)
        match = Match.objects.create(division=division, team1=t1, team2=t2)
        return MatchResultForm({'score': score, 'winner': (t1 if winner_is_t1 else t2).pk}, instance=match)

    def test_form_accepts_capped_set(self):
        form = self._form('21-20, 15-9')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.max_set_score, 21)

    def test_form_rejects_set_over_cap(self):
        self.assertFalse(self._form('22-20, 15-9').is_valid())

    def test_new_tournaments_default_to_3x15(self):
        t = Tournament(name="x", date=datetime.date.today(), division_model='youth')
        self.assertEqual(t.scoring_model, 'best_of_5_15')


# ---------------------------------------------------------------------------
# Several round-robin groups without playoff ('groups')
# ---------------------------------------------------------------------------

from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from players.models import Player, Team  # noqa: E402

from .format_suggestion import suggest_formats  # noqa: E402
from .models import Division  # noqa: E402
from .scheduler import generate_schedule  # noqa: E402


def _singles(n, owner):
    teams = []
    for i in range(n):
        p = Player.objects.create(name=f"Jugador {i:02d}", owner=owner, has_arrived=True)
        teams.append(Team.objects.create(player1=p))
    return teams


class GroupsFormatTest(TestCase):
    def setUp(self):
        self.user = make_user(username='clubgroups')
        self.client = Client()
        self.client.force_login(self.user)
        self.tournament = Tournament.objects.create(
            name="Grups", date=datetime.date.today(), division_model='youth', owner=self.user)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Aleví", discipline='single',
            tournament_type='groups', group_count=2)
        self.division.teams.set(_singles(9, self.user))

    def test_generates_only_group_matches(self):
        matches = generate_schedule(self.division)
        # groups of 5 and 4 → 10 + 6 matches, no playoff
        self.assertEqual(len(matches), 16)
        self.assertTrue(all(m.phase == 'group' for m in matches))
        self.assertEqual(sorted({m.group_number for m in matches}), [1, 2])

    def test_properties(self):
        self.assertTrue(self.division.has_groups)
        self.assertEqual(self.division.advancing_count, 0)
        self.division.tournament_type = 'playoff'
        self.assertEqual(self.division.advancing_count, self.division.advance_count)

    def test_detail_and_public_pages_render_group_standings(self):
        generate_schedule(self.division)
        m = self.division.matches.first()
        m.score, m.winner, m.status = '15-10, 15-8', m.team1, 'completed'
        m.save()
        r = self.client.get(reverse('tournament_detail', args=[self.tournament.pk]))
        self.assertEqual(r.status_code, 200)
        r = self.client.get(reverse('tournament_run', args=[self.tournament.pk]))
        self.assertEqual(r.status_code, 200)
        r = Client().get(reverse('public_tournament', args=[self.tournament.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, m.team1.name)

    def test_medals_one_podium_per_group(self):
        from .views import _compute_medals
        generate_schedule(self.division)
        for m in self.division.matches.all():
            m.score, m.winner, m.status = '15-10, 15-8', m.team1, 'completed'
            m.save()
        gold, silver, _bronze = _compute_medals(self.division)
        self.assertEqual(len(gold), 2)
        self.assertEqual(len(silver), 2)


class FormatSuggestionTest(TestCase):
    def test_small_division_is_single_round_robin(self):
        for n in (3, 5, 6):
            self.assertEqual(suggest_formats(n)[0]['tournament_type'], 'group')

    def test_medium_and_large_divisions(self):
        best = suggest_formats(9)[0]
        self.assertEqual((best['tournament_type'], best['group_count'], best['advance_count']), ('playoff', 2, 2))
        self.assertEqual(best['matches'], 10 + 6 + 3)
        best = suggest_formats(16)[0]
        self.assertEqual((best['tournament_type'], best['group_count']), ('playoff', 4))
        self.assertIn('groups', [o['tournament_type'] for o in suggest_formats(16)])

    def test_apply_format_view(self):
        user = make_user(username='clubfmt')
        c = Client(); c.force_login(user)
        t = Tournament.objects.create(name="F", date=datetime.date.today(), division_model='youth', owner=user)
        d = Division.objects.create(tournament=t, name="Infantil", discipline='single')
        d.teams.set(_singles(12, user))
        r = c.get(reverse('tournament_detail', args=[t.pk]))
        self.assertContains(r, f'/tournaments/division/{d.pk}/format/')
        c.post(reverse('division_apply_format', args=[d.pk]),
               {'tournament_type': 'playoff', 'group_count': 4, 'advance_count': 2})
        d.refresh_from_db()
        self.assertEqual((d.tournament_type, d.group_count, d.advance_count), ('playoff', 4, 2))


# ---------------------------------------------------------------------------
# Catalan
# ---------------------------------------------------------------------------

from django.utils import translation  # noqa: E402

from .templatetags.tournament_extras import bracket_label_i18n  # noqa: E402


class CatalanTest(TestCase):
    def test_bracket_labels_in_catalan(self):
        with translation.override('ca'):
            self.assertEqual(bracket_label_i18n('Nr.1 gr.1 vs Nr.2 gr.2'), '1r del grup 1 vs 2n del grup 2')
            self.assertEqual(bracket_label_i18n('Nr.1 gr.3 (fri)'), '1r del grup 3 (exempt)')
            self.assertEqual(bracket_label_i18n('V-kamp #12 vs V-kamp #13'), 'Guanyador del #12 vs Guanyador del #13')
        with translation.override('en'):
            self.assertEqual(bracket_label_i18n('Nr.2 gr.1'), '2nd of group 1')
        self.assertIsNone(bracket_label_i18n(None))

    def test_player_status_messages_translated(self):
        with translation.override('ca'):
            from django.utils.translation import gettext as _
            self.assertEqual(_('%(name)s has not arrived yet') % {'name': 'Pau'}, "Pau encara no s'ha presentat")

    def test_pages_render_in_catalan(self):
        user = make_user(username='clubca')
        c = Client(); c.force_login(user)
        c.cookies['django_language'] = 'ca'
        t = Tournament.objects.create(name="Tardor", date=datetime.date.today(), division_model='youth', owner=user)
        r = c.get(reverse('tournament_detail', args=[t.pk]))
        self.assertContains(r, 'Crea una categoria nova')
        r = Client(HTTP_ACCEPT_LANGUAGE='ca').get(reverse('public_tournament', args=[t.pk]))
        self.assertContains(r, 'Horari')


# ---------------------------------------------------------------------------
# Public page cache: versioned invalidation and language-aware keys
# ---------------------------------------------------------------------------

from django.core.cache import cache  # noqa: E402
from django.test import override_settings  # noqa: E402

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'club-tests'}}


@override_settings(CACHES=LOCMEM)
class PublicCacheTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user(username='clubcache')
        self.tournament = Tournament.objects.create(
            name="Cache", date=datetime.date.today(), division_model='youth', owner=self.user)
        self.url = reverse('public_tournament', args=[self.tournament.pk])

    def test_second_request_is_a_hit(self):
        self.assertEqual(Client().get(self.url)['X-Cache'], 'MISS')
        self.assertEqual(Client().get(self.url)['X-Cache'], 'HIT')

    def test_changes_invalidate_the_cache_immediately(self):
        Client().get(self.url)
        Division.objects.create(tournament=self.tournament, name="Nova categoria")
        r = Client().get(self.url)
        self.assertEqual(r['X-Cache'], 'MISS')
        self.assertContains(r, "Nova categoria")

    def test_language_choice_gets_its_own_cache_entry(self):
        c_ca = Client(); c_ca.cookies['django_language'] = 'ca'
        c_en = Client(); c_en.cookies['django_language'] = 'en'
        self.assertContains(c_ca.get(self.url), 'Horari')
        r = c_en.get(self.url)
        self.assertEqual(r['X-Cache'], 'MISS')
        self.assertContains(r, 'Time schedule')
