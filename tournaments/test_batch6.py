"""Batch 6: force a start before the rest is over, podium matches, no indexing, plan B
(court sheets and quick result entry), season statistics, school report and demo tournament."""
import datetime
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from players.models import Player

from .models import Division, Match, Tournament, TournamentDay
from .scheduler import generate_schedule
from .test_formats import _teams, play_all
from . import test_live as _tl
from .tests import make_user


class ForceRestTest(_tl.CourtPlanTest):
    def test_start_anyway_when_only_rest_is_missing(self):
        c = self.p['C'].player1
        c.rest_until = timezone.now() + timedelta(minutes=6)
        c.save()
        url = reverse('match_start', args=[self.m2.pk])
        r = self.c.post(url, {'court': '2'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertFalse(r['ok'])
        self.assertTrue(r['can_force'])
        r = self.c.post(url, {'court': '2', 'force_rest': '1'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertTrue(r['ok'])
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'in_progress')

    def test_cannot_force_when_player_is_playing(self):
        self.m1.status, self.m1.court, self.m1.started_at = 'in_progress', '1', timezone.now()
        self.m1.save()
        r = self.c.post(reverse('match_start', args=[self.m4.pk]), {'court': '2', 'force_rest': '1'},
                        HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertFalse(r['ok'])
        self.assertFalse(r['can_force'])

    def test_loudspeaker_offers_start_anyway(self):
        c = self.p['C'].player1
        c.rest_until = timezone.now() + timedelta(minutes=6)
        c.save()
        r = self.c.get(reverse('tournament_run', args=[self.t.pk]))
        self.assertContains(r, 'name="force_rest"')


class PodiumTest(TestCase):
    def setUp(self):
        self.user = make_user(username='clubpodium')
        self.c = Client()
        self.c.force_login(self.user)
        self.t = Tournament.objects.create(name="P", date=timezone.localdate(), division_model='youth',
                                           scoring_model='best_of_5_15', owner=self.user)
        TournamentDay.objects.create(tournament=self.t, day_number=1, date=self.t.date,
                                     start_time=datetime.time(0, 5), court_count=4, buffer_minutes=0)

    def test_stages_of_a_knockout(self):
        from .podium import podium_stage
        d = Division.objects.create(tournament=self.t, name='Cadet', discipline='single', tournament_type='playoff',
                                    group_count=4, advance_count=2, third_place_match=True)
        d.teams.set(_teams(self.user, [(f'C{i:02d}', 'M') for i in range(16)]))
        generate_schedule(d)
        stages = {}
        for m in d.matches.filter(phase='playoff'):
            stages.setdefault(podium_stage(m), 0)
            stages[podium_stage(m)] += 1
        self.assertEqual(stages, {'quarter': 4, 'semi': 2, 'third': 1, 'final': 1})
        self.assertIsNone(podium_stage(d.matches.filter(phase='group').first()))

    def test_final_league_and_badge_on_run_page(self):
        from .podium import podium_stage
        d = Division.objects.create(tournament=self.t, name='Aleví', discipline='single', tournament_type='playoff',
                                    group_count=2, advance_count=1, final_format='league')
        names = [f'A{i}' for i in range(6)]
        d.teams.set(_teams(self.user, [(n, 'M') for n in names]))
        generate_schedule(d)
        self.assertTrue(all(podium_stage(m) == 'league' for m in d.matches.filter(phase='playoff')))
        play_all_groups(d, names)
        for i, m in enumerate(d.matches.order_by('pk'), start=1):
            m.match_number, m.court = i, '1'
            m.save()
        r = self.c.get(reverse('tournament_run', args=[self.t.pk]))
        self.assertContains(r, 'podium-badge')
        self.assertContains(r, 'podium-call')


def play_all_groups(division, strength):
    from .scheduler import advance_bracket, fill_playoff_bracket_from_group
    for m in division.matches.filter(phase='group', status='pending').select_related('team1', 'team2'):
        w1 = strength.index(m.team1.name) < strength.index(m.team2.name)
        m.score = '15-5, 15-6' if w1 else '5-15, 6-15'
        m.winner = m.team1 if w1 else m.team2
        m.status = 'completed'
        m.save()
        advance_bracket(m)
        fill_playoff_bracket_from_group(division, m.group_number)


class NoIndexTest(TestCase):
    def test_robots_and_header(self):
        r = Client().get('/robots.txt')
        self.assertContains(r, 'GPTBot')
        self.assertContains(r, 'User-agent: *\nDisallow: /')
        self.assertIn('noindex', r['X-Robots-Tag'])
        r = Client().get('/')
        self.assertIn('noai', r['X-Robots-Tag'])


class PlanBTest(_tl.CourtPlanTest):
    def test_pages_and_quick_entry(self):
        for name in ('tournament_planb', 'tournament_court_sheets', 'tournament_quick_results'):
            self.assertEqual(self.c.get(reverse(name, args=[self.t.pk])).status_code, 200)
        r = self.c.get(reverse('tournament_court_sheets', args=[self.t.pk]))
        self.assertContains(r, 'class="sheet"', count=2)
        url = reverse('match_quick_result', args=[self.m1.pk])
        r = self.c.post(url, {'s1a': '15', 's1b': '20'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertFalse(r['ok'])
        r = self.c.post(url, {'s1a': '15', 's1b': '10', 's2a': '8', 's2b': '15', 's3a': '15', 's3b': '13'},
                        HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertTrue(r['ok'], r)
        self.m1.refresh_from_db()
        self.assertEqual((self.m1.status, self.m1.winner_id), ('completed', self.m1.team1_id))
        # typed afterwards: nobody gets rest time
        self.assertIsNone(Player.objects.get(pk=self.p['A'].player1.pk).rest_until)


class StatsTest(TestCase):
    def setUp(self):
        self.user = make_user(username='clubstats')
        self.c = Client()
        self.c.force_login(self.user)
        names = ['Anna', 'Bet', 'Cesc', 'Dani']
        self.players = {}
        self.ts = []
        for k, day in enumerate((datetime.date(2026, 10, 3), datetime.date(2027, 1, 16))):
            t = Tournament.objects.create(name=f"T{k}", date=day, division_model='youth', owner=self.user,
                                          scoring_model='best_of_5_15')
            TournamentDay.objects.create(tournament=t, day_number=1, date=day, start_time=datetime.time(9, 0),
                                         court_count=2, buffer_minutes=0)
            d = Division.objects.create(tournament=t, name='Aleví', discipline='single', tournament_type='group',
                                        gold_count=1, silver_count=1, bronze_count=1)
            teams = []
            from players.models import Team
            for i, n in enumerate(names):
                p = self.players.get(n) or Player.objects.create(owner=self.user, name=n, gender='M',
                                                                 school='Escola A' if i % 2 == 0 else 'Escola B')
                self.players[n] = p
                team = Team.objects.filter(player1=p, player2__isnull=True).first() or Team.objects.create(player1=p)
                teams.append(team)
            d.teams.set(teams)
            generate_schedule(d)
            start = timezone.make_aware(datetime.datetime.combine(day, datetime.time(9, 0)))
            for m in d.matches.all():
                m.started_at = start
                m.save(update_fields=['started_at'])
            play_all(d, names)
            for m in d.matches.all():
                Match.objects.filter(pk=m.pk).update(finished_at=start + timedelta(minutes=14))
            self.ts.append(t)

    def test_player_season_panel(self):
        from .stats import player_season_stats
        s = player_season_stats(self.players['Anna'], self.ts[0].date)
        self.assertEqual(s['season'], '2026-27')
        self.assertEqual(s['total']['tournaments'], 2)
        self.assertEqual((s['total']['won'], s['total']['lost']), (6, 0))
        self.assertEqual(s['total']['medals']['gold'], 2)
        r = Client().get(reverse('public_player', args=[self.ts[1].pk, self.players['Anna'].pk]))
        self.assertContains(r, '2026-27')

    def test_club_stats_school_report_and_durations(self):
        r = self.c.get(reverse('club_stats'))
        self.assertContains(r, 'Escola A')
        self.assertEqual(r.context['schools'][0]['name'], 'Escola A')
        self.assertEqual(r.context['durations']['categories'][0]['median'], 14)
        r = self.c.get(reverse('tournament_school_report', args=[self.ts[0].pk]))
        self.assertContains(r, 'Escola B')
        r = self.c.get(reverse('tournament_school_report', args=[self.ts[0].pk]) + '?school=Escola B')
        self.assertNotContains(r, '<b>Anna</b>')


class DemoTest(TestCase):
    def test_create_run_and_delete(self):
        user = make_user(username='clubdemo')
        c = Client()
        c.force_login(user)
        r = c.post(reverse('demo_create'))
        t = Tournament.objects.get(owner=user, is_demo=True)
        self.assertRedirects(r, reverse('tournament_run', args=[t.pk]), fetch_redirect_response=False)
        self.assertTrue(Match.objects.filter(division__tournament=t, scheduled_time__isnull=False).exists())
        self.assertEqual(c.get(reverse('tournament_run', args=[t.pk])).status_code, 200)
        self.assertNotContains(Client().get('/'), t.name)
        # again: replaced, not duplicated
        c.post(reverse('demo_create'))
        self.assertEqual(Tournament.objects.filter(owner=user, is_demo=True).count(), 1)
        self.assertEqual(Player.objects.filter(owner=user).count(), 10)
        c.post(reverse('demo_delete'))
        self.assertFalse(Tournament.objects.filter(owner=user).exists())
        self.assertFalse(Player.objects.filter(owner=user).exists())

    def test_bigscreen_renders(self):
        user = make_user(username='clubdemo2')
        c = Client()
        c.force_login(user)
        c.post(reverse('demo_create'))
        t = Tournament.objects.get(owner=user, is_demo=True)
        r = c.get(reverse('tournament_bigscreen', args=[t.pk]))
        self.assertContains(r, 'match-court')
