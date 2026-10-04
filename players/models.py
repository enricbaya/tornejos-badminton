from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

# Create your models here.

# Default categories used for seeding and pre-population
DEFAULT_DIVISION_CATEGORIES = ['U9', 'U11', 'U13', 'U15', 'U17', 'U19', 'A', 'B', 'C']


class DivisionCategory(models.Model):
    """Store a user-defined division category (for example U9 or A)."""
    name = models.CharField(max_length=30, verbose_name=_("Name"))
    sort_order = models.IntegerField(default=0, verbose_name=_("Sort order"))
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='division_categories',
        verbose_name=_("Club user"),
    )

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        constraints = [
            models.UniqueConstraint(fields=['owner', 'name'], name='unique_owner_category'),
        ]

    def __str__(self):
        """Return the category name.

        Returns:
            str: Category name.
        """
        return self.name


class Player(models.Model):
    """Represent a player participating in tournaments."""

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    age = models.IntegerField(verbose_name=_("Age"), blank=True, null=True)
    GENDER_CHOICES = [
        ('M', _("Male")),
        ('K', _("Female")),
    ]
    division = models.CharField(max_length=30, blank=True, verbose_name=_("Division"))
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, default='M', verbose_name=_("Gender"))
    rest_until = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Resting until"),
        help_text=_("The player is resting and cannot start new matches before this time."),
    )
    has_arrived = models.BooleanField(
        default=False,
        verbose_name=_("Arrived"),
        help_text=_("Whether the player has checked in at the judge table."),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='players',
        verbose_name=_("Club user"),
    )


    class Meta:
        verbose_name = _("Player")
        verbose_name_plural = _("Players")

    def __str__(self):
        """Return a readable player representation.

        Returns:
            str: Player name, gender, and division.
        """
        return f"{self.name} ({self.get_gender_display()}, {self.division})"

class Team(models.Model):
    """Represent a team composed of one or two players."""

    PAIR_TYPE_CHOICES = [
        ('double', _("Doubles")),
        ('mixed', _("Mixed doubles")),
    ]
    player1 = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='team_player1', verbose_name=_("Player 1"))
    player2 = models.ForeignKey(Player, on_delete=models.SET_NULL, related_name='team_player2', null=True, blank=True, verbose_name=_("Player 2"))
    pair_type = models.CharField(
        max_length=10, choices=PAIR_TYPE_CHOICES, null=True, blank=True,
        verbose_name=_("Pair type"),
        help_text=_("Doubles: same gender · Mixed doubles: mixed gender"),
    )
    division = models.CharField(
        max_length=30, blank=True, null=True,
        verbose_name=_("Division"),
    )
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Name"))

    class Meta:
        verbose_name = _("Team")
        verbose_name_plural = _("Teams")

    @property
    def is_single(self):
        """Return whether this team represents a single player.

        Returns:
            bool: ``True`` when ``player2`` is missing, else ``False``.
        """
        return self.player2 is None

    @property
    def players(self):
        """Return the team's non-null players, for iteration in templates.

        Returns:
            list: ``player1`` and ``player2`` (when set), in order.
        """
        return [p for p in (self.player1, self.player2) if p]

    def save(self, *args, **kwargs):
        """Persist the team and auto-generate a name when missing.

        Args:
            *args: Positional arguments passed to ``Model.save``.
            **kwargs: Keyword arguments passed to ``Model.save``.
        """
        if not self.name:
            if self.player2:
                self.name = f"{self.player1.name} & {self.player2.name}"
            else:
                self.name = self.player1.name
        super().save(*args, **kwargs)

    def __str__(self):
        """Return the team name.

        Returns:
            str: Team name.
        """
        return self.name
