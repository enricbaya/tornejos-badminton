from django import forms
from django.utils.translation import gettext_lazy as _
from .models import Player, Team, DivisionCategory


def _division_choices(owner):
    """Build division select choices for an owner.

    Args:
        owner: User owning the available division categories.

    Returns:
        list[tuple[str, str]]: Choice tuples for select widgets.
    """
    cats = DivisionCategory.objects.filter(owner=owner).values_list('name', flat=True)
    return [('', _("— Select age group —"))] + [(c, c) for c in cats]


class PlayerForm(forms.ModelForm):
    """Form used to create and edit players."""

    class Meta:
        model = Player
        fields = ['name', 'age', 'gender', 'division', 'school']
        widgets = {'school': forms.TextInput(attrs={'list': 'school-options', 'autocomplete': 'off'})}

    def __init__(self, *args, owner=None, **kwargs):
        """Configure owner-specific division choices.

        Args:
            *args: Positional arguments passed to ``ModelForm``.
            owner: User used to scope available division categories.
            **kwargs: Keyword arguments passed to ``ModelForm``.
        """
        super().__init__(*args, **kwargs)
        self.school_options = []
        if owner is not None:
            self.fields['division'].widget = forms.Select(choices=_division_choices(owner))
            self.school_options = school_options(owner)

    def clean_school(self):
        value = ' '.join(self.cleaned_data.get('school', '').split())
        # Reuse the existing spelling when only upper/lower case differs.
        for existing in self.school_options:
            if existing.lower() == value.lower():
                return existing
        return value


def school_options(owner):
    """Schools/clubs already used by this club's players, alphabetically."""
    names = Player.objects.filter(owner=owner).exclude(school='').values_list('school', flat=True).distinct()
    return sorted(set(names), key=str.lower)


class TeamForm(forms.ModelForm):
    """Form used to create and edit doubles or mixed teams."""

    class Meta:
        model = Team
        fields = ['player1', 'player2', 'pair_type', 'division']

    def __init__(self, *args, owner=None, **kwargs):
        """Limit selectable players and divisions to the owner.

        Args:
            *args: Positional arguments passed to ``ModelForm``.
            owner: User used to scope available players and divisions.
            **kwargs: Keyword arguments passed to ``ModelForm``.
        """
        super().__init__(*args, **kwargs)
        if owner is not None:
            qs = Player.objects.filter(owner=owner).order_by('name')
            self.fields['player1'].queryset = qs
            self.fields['player2'].queryset = qs
            self.fields['division'].widget = forms.Select(choices=_division_choices(owner))

    def clean(self):
        """Validate pair type and gender constraints for two-player teams.

        Returns:
            dict: Cleaned form data.

        Raises:
            ValidationError: If pair type or gender composition is invalid.
        """
        cleaned = super().clean()
        p1 = cleaned.get('player1')
        p2 = cleaned.get('player2')
        pair_type = cleaned.get('pair_type')

        if p2:
            if not pair_type:
                self.add_error('pair_type', _("Select pair type (doubles or mixed doubles) when adding two players."))
            elif p1 and pair_type == 'double' and p1.gender != p2.gender:
                self.add_error('pair_type', _("Doubles requires two players of the same gender."))
            elif p1 and pair_type == 'mixed' and p1.gender == p2.gender:
                self.add_error('pair_type', _("Mixed doubles requires one male and one female player."))
        else:
            # Singles don't need a pair_type
            cleaned['pair_type'] = None

        return cleaned
