"""Flexible final phases: final round-robin, cross-over knockout with everybody,
third-place match and separate finals per gender (club scenarios of October 2026)."""
import datetime

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from players.models import Player, Team

from .models import Division, Match, Tournament, TournamentDay
from .scheduler import advance_bracket, fill_playoff_bracket_from_group, generate_schedule, get_bracket_data
from .tests import make_user


def _teams(owner, spec):
    """spec: list of (name, gender)"""
    return [Team.objects.create(player1=Player.objects.create(owner=owner, name=n, gender=g, has_arrived=True))
            for n, g in spec]


def play_all(division, strength):
    """Play every playable match; the stronger name (lower index in strength) wins."""
    for _ in range(50):
        pending = [m for m in division.matches.filter(status='pending')
                   .select_related('team1', 'team2') if m.team1_id and m.team2_id]
        if not pending:
            return
        for m in pending:
            w1 = strength.index(m.team1.name) < strength.index(m.team2.name)
            m.score = '15-5, 15-6' if w1 else '5-15, 6-15'
            m.winner = m.team1 if w1 else m.team2
            m.status = 'completed'
            m.save()
            advance_bracket(m)
            if m.phase == 'group':
                fill_playoff_bracket_from_group(division, m.group_number)


class BaseFormat(TestCase):
    def setUp(self):
        self.user = make_user(username='clubformats')
        self.t = Tournament.objects.create(name="Formats", date=timezone.localdate(), division_model='youth',
                                           scoring_model='best_of_5_15', owner=self.user)
        TournamentDay.objects.create(tournament=self.t, day_number=1, date=self.t.date,
                                     start_time=datetime.time(9, 0), court_count=4, buffer_minutes=0)

    def per_player(self, division):
        count = {}
        for m in division.matches.filter(status='completed').exclude(team2__isnull=True):
            for t in (m.team1, m.team2):
                count[t.name] = count.get(t.name, 0) + 1
        return count


class FinalLeagueTest(BaseFormat):
    """Aleví/Infantil: 3 groups of 4, then a final round-robin of the 4 best (3 winners + best runner-up)."""

    def test_flow(self):
        names = [f'P{i:02d}' for i in range(12)]
        d = Division.objects.create(tournament=self.t, name='Aleví/Infantil', discipline='single',
                                    tournament_type='playoff', group_count=3, final_format='league',
                                    qualify_mode='total', final_qualifiers=4)
        d.teams.set(_teams(self.user, [(n, 'M') for n in names]))
        generate_schedule(d)
        self.assertEqual(d.matches.filter(phase='group').count(), 18)
        self.assertEqual(d.matches.filter(phase='playoff').count(), 6)
        self.assertTrue(all(m.bracket_label.startswith('Top.') for m in d.matches.filter(phase='playoff')))
        play_all(d, names)
        self.assertFalse(d.matches.filter(status='pending').exists())
        counts = self.per_player(d)
        self.assertEqual(max(counts.values()), 6)
        self.assertEqual(min(counts.values()), 3)
        from .final_phase import section_podiums
        pod = section_podiums(d)[0]
        self.assertEqual(pod['gold'][0].name, 'P00')
        bd = get_bracket_data(d)
        self.assertEqual(len(bd['sections'][0]['league_rows']), 4)


class CrossKnockoutTest(BaseFormat):
    """Cadet: 2 groups of 4, everybody goes through crossed (1A-4B, 2A-3B…), QF, SF, final + 3rd place."""

    def test_flow(self):
        names = [f'C{i}' for i in range(8)]
        d = Division.objects.create(tournament=self.t, name='Cadet', discipline='single', tournament_type='playoff',
                                    group_count=2, advance_count=4, third_place_match=True)
        d.teams.set(_teams(self.user, [(n, 'M') for n in names]))
        generate_schedule(d)
        r1 = sorted(d.matches.filter(phase='playoff', is_third_place=False).values_list('match_round', flat=True))[0]
        labels = set(d.matches.filter(phase='playoff', match_round=r1).values_list('bracket_label', flat=True))
        self.assertIn('Nr.1 gr.1 vs Nr.4 gr.2', labels)
        self.assertIn('Nr.2 gr.1 vs Nr.3 gr.2', labels)
        self.assertEqual(d.matches.filter(is_third_place=True).count(), 1)
        play_all(d, names)
        self.assertFalse(d.matches.filter(status='pending').exists())
        third = d.matches.get(is_third_place=True)
        self.assertTrue(third.team1_id and third.team2_id and third.winner_id)
        from .views import _compute_medals
        gold, silver, bronze = _compute_medals(d)
        self.assertEqual((gold[0].name, len(bronze)), ('C0', 1))
        counts = self.per_player(d)
        self.assertEqual(max(counts.values()), 6)
        self.assertEqual(min(counts.values()), 4)


class SplitFinalTest(BaseFormat):
    """Sènior: 6 mixed groups of 3; boys semi-finals + final + 3rd place, girls final + 3rd place (3rd vs 4th)."""

    def test_flow(self):
        boys = [f'M{i:02d}' for i in range(10)]
        girls = [f'F{i}' for i in range(8)]
        d = Division.objects.create(tournament=self.t, name='Sènior', discipline='single', tournament_type='playoff',
                                    group_count=6, final_split='gender', third_place_match=True,
                                    section_qualifiers={'M': 4, 'K': 2})
        d.teams.set(_teams(self.user, [(n, 'M') for n in boys] + [(n, 'K') for n in girls]))
        generate_schedule(d)
        self.assertEqual(d.matches.filter(phase='group').count(), 18)
        self.assertEqual(d.matches.filter(phase='playoff', final_section='M').count(), 4)   # 2 SF, final, 3rd
        self.assertEqual(d.matches.filter(phase='playoff', final_section='K').count(), 2)   # final, 3rd (3 vs 4)
        play_all(d, boys + girls)
        self.assertFalse(d.matches.filter(status='pending').exists())
        from .final_phase import section_podiums
        pods = {p['code']: p for p in section_podiums(d)}
        self.assertEqual(pods['M']['gold'][0].name, 'M00')
        self.assertTrue(pods['K']['gold'][0].name.startswith('F'))
        self.assertTrue(all(t.name.startswith('F') for m in d.matches.filter(final_section='K')
                            for t in (m.team1, m.team2)))
        self.assertEqual(len(pods['K']['bronze']), 1)
        from .views import _division_medal_context
        ctx = _division_medal_context(d)
        self.assertEqual(len(ctx['subgroups']), 2)
        # time schedule handles the separate brackets
        Match.objects.filter(division=d).update(status='pending', winner=None, score=None)
        from .schedule_planner import generate_time_schedule
        for i, m in enumerate(Match.objects.filter(division=d).order_by('pk'), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        self.assertGreater(generate_time_schedule(self.t), 0)


class SmallerGroupsByeTest(BaseFormat):
    """Everybody goes through but one group is smaller: the missing position becomes a bye."""

    def test_missing_position_is_a_bye(self):
        names = [f'S{i}' for i in range(7)]
        d = Division.objects.create(tournament=self.t, name='X', discipline='single', tournament_type='playoff',
                                    group_count=2, advance_count=4)
        d.teams.set(_teams(self.user, [(n, 'M') for n in names]))
        generate_schedule(d)
        play_all(d, names)
        self.assertFalse(d.matches.filter(status='pending').exists())
        final = d.matches.filter(phase='playoff').order_by('-match_round').first()
        self.assertEqual(final.winner.name, 'S0')


class PagesRenderTest(BaseFormat):
    def test_pages_with_split_final(self):
        c = Client()
        c.force_login(self.user)
        d = Division.objects.create(tournament=self.t, name='Sènior', discipline='single', tournament_type='playoff',
                                    group_count=2, final_split='gender', third_place_match=True,
                                    section_qualifiers={'M': 4, 'K': 2})
        d.teams.set(_teams(self.user, [(f'M{i}', 'M') for i in range(4)] + [(f'F{i}', 'K') for i in range(4)]))
        generate_schedule(d)
        for url in (reverse('tournament_detail', args=[self.t.pk]), reverse('tournament_run', args=[self.t.pk]),
                    reverse('division_medals', args=[d.pk]), reverse('division_medals_edit', args=[d.pk])):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(Client().get(reverse('public_tournament', args=[self.t.pk])).status_code, 200)


class ConfiguratorTest(BaseFormat):
    def test_evaluate_club_formats(self):
        from .format_suggestion import evaluate_format, suggest_flexible
        e = evaluate_format({'group_count': 3, 'final_format': 'league', 'qualify_mode': 'total',
                             'final_qualifiers': 4}, 12)
        self.assertEqual((e['matches'], e['min_matches_per_team'], e['max_matches_per_team']), (24, 3, 6))
        e = evaluate_format({'group_count': 2, 'final_format': 'knockout', 'advance_count': 4,
                             'third_place_match': True}, 8)
        self.assertEqual((e['matches'], e['min_matches_per_team'], e['max_matches_per_team']), (20, 4, 6))
        e = evaluate_format({'group_count': 6, 'final_format': 'knockout', 'final_split': 'gender',
                             'third_place_match': True, 'section_qualifiers': {'M': 4, 'K': 2}}, 18, {'M': 10, 'K': 8})
        self.assertEqual((e['matches'], e['min_matches_per_team'], e['max_matches_per_team']), (24, 2, 4))
        for n in (8, 12, 16):
            best = suggest_flexible(n)[0]
            self.assertTrue(best['ok'], (n, best['label']))

    def test_apply_and_preview(self):
        c = Client()
        c.force_login(self.user)
        d = Division.objects.create(tournament=self.t, name='Sènior', discipline='single')
        d.teams.set(_teams(self.user, [(f'M{i}', 'M') for i in range(10)] + [(f'F{i}', 'K') for i in range(8)]))
        params = {'group_count': 6, 'final_format': 'knockout', 'final_split': 'gender', 'third_place_match': '1',
                  'sq_M': 4, 'sq_K': 2}
        r = c.get(reverse('division_format_preview', args=[d.pk]), params)
        self.assertEqual(r.json()['matches'], 24)
        c.post(reverse('division_apply_format', args=[d.pk]), params)
        d.refresh_from_db()
        self.assertEqual((d.tournament_type, d.group_count, d.final_split, d.section_qualifiers, d.third_place_match),
                         ('playoff', 6, 'gender', {'M': 4, 'K': 2}, True))
        r = c.get(reverse('tournament_detail', args=[self.t.pk]))
        self.assertContains(r, 'fmt-form')
        # programme generated but nothing played: the format can still be changed
        generate_schedule(d)
        c.post(reverse('division_apply_format', args=[d.pk]), {'group_count': 3, 'final_format': 'none'})
        d.refresh_from_db()
        self.assertEqual((d.tournament_type, d.matches.count()), ('groups', 0))


class InterleavedScheduleTest(TestCase):
    """The trial tournament of December 2026: 65 matches, 6 courts, 9:00–14:00. Everything must fit and
    the senior finals (boys and girls) come last."""

    def test_trial_tournament_fits(self):
        user = make_user(username='clubsched')
        t = Tournament.objects.create(name="Nadal", date=datetime.date(2026, 12, 21), division_model='youth',
                                      scoring_model='best_of_5_15', owner=user, single_match_duration=20,
                                      player_break_time=15)
        TournamentDay.objects.create(tournament=t, day_number=1, date=t.date, start_time=datetime.time(9, 0),
                                     end_time=datetime.time(14, 0), court_count=6, buffer_minutes=15)
        a = Division.objects.create(tournament=t, name='Alevins', discipline='single', tournament_type='playoff',
                                    group_count=3, advance_count=1, final_format='league', schedule_priority=1)
        a.teams.set(_teams(user, [(f'A{i:02d}', 'M') for i in range(12)]))
        c = Division.objects.create(tournament=t, name='Cadet', discipline='single', tournament_type='playoff',
                                    group_count=2, advance_count=4, third_place_match=True, schedule_priority=2)
        c.teams.set(_teams(user, [(f'C{i}', 'M') for i in range(8)]))
        s = Division.objects.create(tournament=t, name='Sènior', discipline='single', tournament_type='playoff',
                                    group_count=6, final_split='gender', third_place_match=True,
                                    section_qualifiers={'M': 4, 'K': 2}, schedule_priority=3)
        s.teams.set(_teams(user, [(f'M{i:02d}', 'M') for i in range(11)] + [(f'F{i}', 'K') for i in range(7)]))
        for d in (a, c, s):
            generate_schedule(d)
        for i, m in enumerate(Match.objects.filter(division__tournament=t).order_by('pk'), start=1):
            m.match_number = i
            m.save(update_fields=['match_number'])
        from .schedule_planner import generate_time_schedule
        generate_time_schedule(t)
        qs = Match.objects.filter(division__tournament=t).exclude(walkover=True)
        self.assertFalse(qs.filter(scheduled_time__isnull=True).exists())
        last = qs.order_by('-scheduled_time').first().scheduled_time
        finals = s.matches.filter(phase='playoff').filter(is_third_place=False, match_round=5, final_section='M')
        self.assertEqual(finals.first().scheduled_time, last)
        girls_final = s.matches.get(final_section='K', is_third_place=False)
        boys_semis_end = max(m.scheduled_time for m in s.matches.filter(final_section='M', match_round=4, is_third_place=False))
        self.assertGreater(girls_final.scheduled_time, boys_semis_end)
        # no player plays two matches at once or without the break
        from collections import defaultdict
        slots = defaultdict(list)
        for m in qs.exclude(team1__isnull=True).exclude(team2__isnull=True):
            for team in (m.team1, m.team2):
                slots[team.player1_id].append(m.scheduled_time)
        for times in slots.values():
            times.sort()
            for x, y in zip(times, times[1:]):
                self.assertGreaterEqual((y - x).total_seconds(), (20 + 15) * 60)
