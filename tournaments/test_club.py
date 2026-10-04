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
