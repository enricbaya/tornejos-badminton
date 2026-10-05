"""Tournament-run page: the court plan (next match per court, advanced matches), live refresh,
starting from the loudspeaker panel, and per-category wished matches per player."""
import datetime
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from players.models import Player, Team

from .models import Division, Match, Tournament, TournamentDay
from .tests import make_user


def _team(owner, name):
    return Team.objects.create(player1=Player.objects.create(owner=owner, name=name, gender='M', has_arrived=True))


class CourtPlanTest(TestCase):
    def setUp(self):
        self.user = make_user(username='clublive')
        self.c = Client()
        self.c.force_login(self.user)
        self.t = Tournament.objects.create(name="Live", date=timezone.localdate(), division_model='youth',
                                           scoring_model='best_of_5_15', owner=self.user, player_break_time=10)
        TournamentDay.objects.create(tournament=self.t, day_number=1, date=self.t.date,
                                     start_time=datetime.time(0, 5), court_count=2, buffer_minutes=0)
        self.d = Division.objects.create(tournament=self.t, name='Aleví', discipline='single', tournament_type='group')
        self.p = {n: _team(self.user, n) for n in 'ABCDEF'}
        self.d.teams.set(self.p.values())
        now = timezone.now()
        mk = lambda n, a, b, court, mins: Match.objects.create(
            division=self.d, team1=self.p[a], team2=self.p[b], match_number=n, court=str(court),
            scheduled_time=now + timedelta(minutes=mins))
        self.m1 = mk(1, 'A', 'B', 1, 0)
        self.m2 = mk(2, 'C', 'D', 2, 0)
        self.m3 = mk(3, 'E', 'F', 1, 20)
        self.m4 = mk(4, 'A', 'C', 2, 40)

    def plan(self):
        from .live_info import court_plan
        return {e['court']: e for e in court_plan(self.t)}

    def test_free_courts_get_their_planned_match(self):
        plan = self.plan()
        self.assertEqual(plan[1]['proposal'], self.m1)
        self.assertEqual(plan[2]['proposal'], self.m2)
        self.assertFalse(plan[1]['advanced'] or plan[2]['advanced'])

    def test_blocked_planned_match_gives_a_marked_advanced_match(self):
        # C is still resting: court 2's planned match (#2) cannot start, #3 is advanced to court 2
        c = self.p['C'].player1
        c.rest_until = timezone.now() + timedelta(minutes=6)
        c.save()
        plan = self.plan()
        self.assertEqual(plan[2]['due'], self.m2)
        self.assertTrue(plan[2]['advanced'])
        self.assertEqual(plan[2]['proposal'], self.m3)
        self.assertTrue(any('C' in b for b in plan[2]['due_blockers']))
        r = self.c.get(reverse('tournament_run', args=[self.t.pk]))
        self.assertContains(r, 'adv-badge')

    def test_busy_court_shows_next_planned(self):
        self.m1.status, self.m1.started_at = 'in_progress', timezone.now()
        self.m1.save()
        plan = self.plan()
        self.assertEqual(plan[1]['active'], self.m1)
        self.assertEqual(plan[1]['due'], self.m3)
        self.assertIsNone(plan[1]['proposal'])

    def test_live_endpoint_and_start_from_call(self):
        r = self.c.get(reverse('tournament_live', args=[self.t.pk]))
        data = r.json()
        self.assertIn('Aleví', data['side'])
        self.assertIn('court-indicators', data['courts'])
        # called → start on the court straight from the loudspeaker box (players are checked in)
        a = self.p['A'].player1
        a.has_arrived = False
        a.save()
        self.c.post(reverse('match_mark_called', args=[self.m1.pk]))
        r = self.c.get(reverse('tournament_live', args=[self.t.pk]))
        self.assertIn('call-start', r.json()['side'])
        self.c.post(reverse('match_start', args=[self.m1.pk]),
                    {'court': '1', 'mark_arrived': '1', 'next': reverse('tournament_run', args=[self.t.pk])})
        self.m1.refresh_from_db()
        self.assertEqual((self.m1.status, self.m1.court), ('in_progress', '1'))
        self.assertTrue(Player.objects.get(pk=a.pk).has_arrived)
        r = self.c.get(reverse('tournament_live', args=[self.t.pk]))
        self.assertEqual(r.json()['occupied'], ['1'])

    def test_court_referee_page_uses_the_same_plan(self):
        from .court_access import court_path, ensure_key
        self.t.court_access_enabled = True
        self.t.save()
        ensure_key(self.t)
        c = self.p['C'].player1
        c.rest_until = timezone.now() + timedelta(minutes=6)
        c.save()
        r = Client().get(court_path(self.t, 2))
        self.assertEqual(r.context['choices'][0], self.m3)
        self.assertTrue(r.context['choices'][0].is_advanced)


class TargetRangeTest(TestCase):
    def test_suggestions_follow_category_range(self):
        from .format_suggestion import suggest_flexible
        best = suggest_flexible(12, target=(5, 7))[0]
        self.assertGreaterEqual(best['min_matches_per_team'], 5)
        self.assertTrue(best['ok'])
        user = make_user(username='clubrange')
        c = Client()
        c.force_login(user)
        t = Tournament.objects.create(name="R", date=timezone.localdate(), division_model='youth', owner=user)
        d = Division.objects.create(tournament=t, name='X', discipline='single')
        d.teams.set([_team(user, f'P{i}') for i in range(12)])
        c.post(reverse('division_apply_format', args=[d.pk]),
               {'group_count': 2, 'final_format': 'none', 'target_min': 4, 'target_max': 6})
        d.refresh_from_db()
        self.assertEqual((d.target_min_matches, d.target_max_matches), (4, 6))
        r = c.get(reverse('tournament_detail', args=[t.pk]))
        self.assertContains(r, 'name="target_min"')
