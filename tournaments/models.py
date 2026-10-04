from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class UserProfile(models.Model):
    """Store per-club preferences such as default language and tier."""
    LANGUAGE_CHOICES = [
        ('ca', 'Català'),
        ('da', 'Dansk'),
        ('en', 'English'),
    ]

    TIER_CHOICES = [
        ('small', _("Small")),
        ('medium', _("Medium")),
        ('large', _("Large")),
        ('unlimited', _("Unlimited")),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
        verbose_name=_("User"),
    )
    language = models.CharField(
        max_length=10,
        choices=LANGUAGE_CHOICES,
        default='ca',
        verbose_name=_("Default language"),
    )
    tier = models.CharField(
        max_length=10,
        choices=TIER_CHOICES,
        default='small',
        verbose_name=_("Club tier"),
        help_text=_("Determines how many players the club may register."),
    )

    def player_limit(self):
        """Return the maximum number of players allowed for this tier, or None if unlimited.

        Reads limits from settings.CLUB_TIER_LIMITS. A value of 0 means unlimited.
        """
        from django.conf import settings
        limit = settings.CLUB_TIER_LIMITS.get(self.tier, 0)
        return limit if limit > 0 else None

    def __str__(self):
        """Return a readable profile label.

        Returns:
            str: Username suffixed with ``profile``.
        """
        return f"{self.user.username} profile"

    class Meta:
        verbose_name = _("User profile")
        verbose_name_plural = _("User profiles")


# Create your models here.

class Tournament(models.Model):
    """Store tournament metadata and scheduling configuration."""

    name = models.CharField(max_length=200, verbose_name=_("Name"))
    date = models.DateField(verbose_name=_("Date"))
    DIVISION_MODEL_CHOICES = [
        ('youth', _("Youth divisions (U9-U19)")),
        ('mixed', _("Mixed divisions (A, B, C)")),
    ]
    division_model = models.CharField(max_length=10, choices=DIVISION_MODEL_CHOICES, verbose_name=_("Division model"))
    SCORING_MODEL_CHOICES = [
        ('best_of_3_21', _("Best of 3 sets to 21")),
        ('best_of_5_15', _("Best of 3 sets to 15")),
    ]
    scoring_model = models.CharField(
        max_length=20,
        choices=SCORING_MODEL_CHOICES,
        default='best_of_5_15',
        verbose_name=_("Scoring model")
    )
    single_match_duration = models.IntegerField(
        help_text=_("Duration of a singles match in minutes"),
        default=30,
        verbose_name=_("Singles match duration")
    )
    double_match_duration = models.IntegerField(
        help_text=_("Duration of a doubles match in minutes"),
        default=40,
        verbose_name=_("Doubles match duration")
    )
    player_break_time = models.IntegerField(
        help_text=_("Minimum rest period for players between matches in minutes"),
        default=15,
        verbose_name=_("Player break time")
    )
    schedule_locked = models.BooleanField(
        default=False,
        verbose_name=_("Schedule locked"),
        help_text=_("When locked, the match schedule cannot be changed."),
    )
    logo = models.ImageField(
        upload_to='tournament_logos/',
        null=True, blank=True,
        verbose_name=_("Logo"),
        help_text=_("Logo is shown on printed programmes and schedules."),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='tournaments',
        verbose_name=_("Club user"),
    )

    def sync_date(self):
        """Synkroniser tournament.date til første dags dato."""
        first = self.days.order_by('date').first()
        if first:
            self.date = first.date

    def __str__(self):
        """Return a readable tournament label.

        Returns:
            str: Tournament name and scoring model.
        """
        return f"{self.name} ({self.scoring_model})"


class TournamentDay(models.Model):
    """Represent a single day within a multi-day tournament."""

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name='days',
        verbose_name=_("Tournament"),
    )
    day_number = models.PositiveIntegerField(
        verbose_name=_("Day number"),
        help_text=_("1-based display number for this day."),
    )
    date = models.DateField(verbose_name=_("Date"))
    start_time = models.TimeField(verbose_name=_("Start time"))
    end_time = models.TimeField(
        null=True, blank=True,
        verbose_name=_("End time"),
        help_text=_(
            "Leave blank for unlimited ending (day runs until all matches complete)."
        ),
    )
    court_count = models.IntegerField(
        default=4,
        verbose_name=_("Number of courts"),
        help_text=_("Number of courts available on this day."),
    )
    buffer_minutes = models.IntegerField(
        default=30,
        verbose_name=_("Buffer (minutes)"),
        help_text=_(
            "Time reserved at the end of the day to absorb delays. "
            "Matches are not placed in this period."
        ),
    )

    class Meta:
        ordering = ['date', 'start_time']
        unique_together = [('tournament', 'day_number')]
        verbose_name = _("Tournament day")
        verbose_name_plural = _("Tournament days")

    def clean(self):
        super().clean()
        if self.day_number is not None and self.day_number < 1:
            raise ValidationError(_("Day number must be 1 or greater."))
        if self.buffer_minutes is not None and self.buffer_minutes < 0:
            raise ValidationError(_("Buffer must not be negative."))
        if self.end_time and self.start_time:
            if self.end_time <= self.start_time:
                raise ValidationError(_("End time must be after start time."))
            start_mins = self.start_time.hour * 60 + self.start_time.minute
            end_mins = self.end_time.hour * 60 + self.end_time.minute
            if self.buffer_minutes is not None and self.buffer_minutes >= (end_mins - start_mins):
                raise ValidationError(
                    _("Buffer is larger than or equal to the available time window.")
                )

    def get_scheduled_match_count(self):
        """Returnerer antal schedulerede matches på denne dag."""
        from .models import Match
        return Match.objects.filter(
            division__tournament=self.tournament,
            scheduled_time__date=self.date,
            scheduled_time__isnull=False,
        ).count()

    def get_expected_match_count(self):
        """Returnerer forventet antal matches baseret på tildelinger."""
        count = 0
        for div in self.group_divisions.all():
            count += div.estimate_group_match_count()
        for div in self.playoff_divisions.all():
            count += div.estimate_playoff_match_count()
        return count

    def __str__(self):
        from django.utils.translation import gettext
        if self.end_time:
            return gettext("Day %(n)s – %(date)s (%(start)s–%(end)s, %(courts)s courts, %(buffer)s min. buffer)") % {
                'n': self.day_number, 'date': self.date, 'start': self.start_time.strftime('%H:%M'),
                'end': self.end_time.strftime('%H:%M'), 'courts': self.court_count, 'buffer': self.buffer_minutes,
            }
        return gettext("Day %(n)s – %(date)s (from %(start)s, %(courts)s courts, open end)") % {
            'n': self.day_number, 'date': self.date, 'start': self.start_time.strftime('%H:%M'),
            'courts': self.court_count,
        }


class Division(models.Model):
    """Represent a playable division within a tournament."""

    DISCIPLINE_CHOICES = [
        ('single', _("Singles")),
        ('double', _("Doubles")),
        ('mixed', _("Mixed doubles")),
    ]
    TOURNAMENT_TYPE_CHOICES = [
        ('group', _("Group (round-robin)")),
        ('groups', _("Several groups (round-robin, no playoff)")),
        ('playoff', _("Group + playoff bracket")),
        ('tree', _("Single elimination")),
    ]
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name='divisions')
    name = models.CharField(max_length=100, verbose_name=_("Division name"))
    discipline = models.CharField(
        max_length=10, choices=DISCIPLINE_CHOICES, default='single',
        verbose_name=_("Discipline")
    )
    tournament_type = models.CharField(
        max_length=10, choices=TOURNAMENT_TYPE_CHOICES, default='group',
        verbose_name=_("Tournament type")
    )
    group_count = models.IntegerField(
        default=2, verbose_name=_("Number of groups"),
        help_text=_("Number of groups in the group stage (only for formats with several groups)."),
    )
    advance_count = models.IntegerField(
        default=2, verbose_name=_("Players advancing per group"),
        help_text=_("Number of players/teams advancing from each group to the playoff bracket."),
    )
    schedule_priority = models.IntegerField(
        default=5,
        verbose_name=_("Schedule priority"),
        help_text=_("1 = schedule earliest · 10 = schedule latest. Use this to ensure younger divisions finish first."),
    )
    group_day = models.ForeignKey(
        'TournamentDay', null=True, blank=True,
        related_name='group_divisions', on_delete=models.SET_NULL,
        verbose_name=_("Group phase day"),
        help_text=_("Day where group matches are played. Empty = scheduler decides freely."),
    )
    playoff_day = models.ForeignKey(
        'TournamentDay', null=True, blank=True,
        related_name='playoff_divisions', on_delete=models.SET_NULL,
        verbose_name=_("Playoff day"),
        help_text=_("Day where playoff matches are played. Empty = scheduler decides freely."),
    )
    gold_count = models.IntegerField(
        default=1, verbose_name=_("Gold medals"),
        help_text=_("Number of teams awarded gold (typically 1)."),
    )
    silver_count = models.IntegerField(
        default=1, verbose_name=_("Silver medals"),
        help_text=_("Number of teams awarded silver (typically 1)."),
    )
    bronze_count = models.IntegerField(
        default=0, verbose_name=_("Bronze medals"),
        help_text=_("0, 1 or 2. Use 2 for bracket tournaments where both semi-final losers receive bronze."),
    )
    teams = models.ManyToManyField('players.Team', related_name='divisions', blank=True, verbose_name=_("Participants"))

    @property
    def has_groups(self):
        """True for formats with several round-robin groups (with or without playoff)."""
        return self.tournament_type in ('groups', 'playoff')

    @property
    def advancing_count(self):
        """Number of teams per group that advance to a playoff (0 when there is no playoff)."""
        return self.advance_count if self.tournament_type == 'playoff' else 0

    def clean(self):
        super().clean()
        if (self.group_day_id and self.playoff_day_id and
                self.group_day_id == self.playoff_day_id and
                self.tournament_type == 'playoff'):
            raise ValidationError(
                _("Group phase day and playoff day cannot be the same day.")
            )

    def estimate_group_match_count(self):
        """Estimerer antal gruppekampe i denne division."""
        if self.tournament_type == 'tree':
            return 0
        groups = self.group_set.all()
        if not groups.exists():
            return 0
        total = 0
        for group in groups:
            n = group.team_set.count()
            if n > 1:
                total += n * (n - 1) // 2
        return total

    def estimate_playoff_match_count(self):
        """Estimerer antal slutspilskampe (worst-case: alle gruppevindere + single-elim)."""
        if self.tournament_type == 'group':
            return 0
        groups = self.group_set.all()
        if not groups.exists():
            return 0
        n = groups.count()
        return max(0, n - 1) if n > 1 else 0

    def __str__(self):
        """Return a readable division label.

        Returns:
            str: Division name, discipline, and tournament name.
        """
        return f"{self.name} – {self.get_discipline_display()} ({self.tournament.name})"

class Match(models.Model):
    """Represent a scheduled or completed match in a division."""

    STATUS_CHOICES = [
        ('pending', _('Pending')),
        ('in_progress', _('In Progress')),
        ('completed', _('Completed')),
    ]
    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name='matches')
    team1 = models.ForeignKey('players.Team', on_delete=models.SET_NULL, null=True, blank=True, related_name='team1_matches')
    team2 = models.ForeignKey('players.Team', on_delete=models.SET_NULL, null=True, blank=True, related_name='team2_matches')
    winner = models.ForeignKey('players.Team', on_delete=models.SET_NULL, null=True, blank=True, related_name='won_matches', verbose_name=_("Winner"))
    score = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Score"))
    match_round = models.IntegerField(default=1, verbose_name=_("Round"))
    match_number = models.IntegerField(null=True, blank=True, verbose_name=_("Match Number"))
    bracket_slot = models.IntegerField(null=True, blank=True, verbose_name=_("Bracket Slot"))
    bracket_label = models.CharField(max_length=120, null=True, blank=True, verbose_name=_("Bracket Label"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name=_("Status"))
    walkover = models.BooleanField(default=False, verbose_name=_("Walk-over"))
    scheduled_time = models.DateTimeField(verbose_name=_("Scheduled Time"), null=True, blank=True)
    court = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Court"))
    started_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Started at"))
    group_number = models.IntegerField(null=True, blank=True, verbose_name=_("Group number"))
    phase = models.CharField(
        max_length=10, default='group',
        choices=[('group', _("Group stage")), ('playoff', _("Playoff bracket"))],
        verbose_name=_("Phase"),
    )

    def __str__(self):
        """Return a readable match label.

        Returns:
            str: Match round and participant labels.
        """
        t1 = self.team1 or self.bracket_label or 'TBD'
        t2 = self.team2 or ('Bye' if not self.bracket_label else 'TBD')
        return f"R{self.match_round}: {t1} vs {t2} ({self.division.name})"


class DivisionSeed(models.Model):
    """Record the optional seed number of a team within a division."""
    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name='seeds')
    team = models.ForeignKey('players.Team', on_delete=models.CASCADE, related_name='division_seeds')
    seed_number = models.PositiveIntegerField(verbose_name=_("Seed number"))

    class Meta:
        unique_together = [('division', 'team'), ('division', 'seed_number')]
        ordering = ['seed_number']

    def __str__(self):
        """Return the seed display label.

        Returns:
            str: Seed number, team name, and division name.
        """
        return f"Seed {self.seed_number}: {self.team.name} ({self.division.name})"


class MedalOverride(models.Model):
    """Store a manual medal assignment overriding computed results."""
    MEDAL_CHOICES = [
        ('gold',   _("Gold")),
        ('silver', _("Silver")),
        ('bronze', _("Bronze")),
    ]
    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name='medal_overrides')
    medal = models.CharField(max_length=10, choices=MEDAL_CHOICES)
    team = models.ForeignKey('players.Team', on_delete=models.CASCADE, related_name='medal_overrides')
    order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        unique_together = [('division', 'medal', 'order')]
        ordering = ['medal', 'order']

    def __str__(self):
        """Return the medal override display label.

        Returns:
            str: Medal, team name, and division name.
        """
        return f"{self.get_medal_display()}: {self.team.name} ({self.division.name})"
