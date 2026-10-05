"""Online registration: a public form for families/schools and the club's review page."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from players.forms import school_options
from players.models import DivisionCategory, Player, Team

from .models import Division, Registration, Tournament
from .public_views import _activate_club_language

MAX_PENDING = 400          # per tournament, to stop runaway spam
MAX_PER_IP_PER_HOUR = 40


class RegistrationForm(forms.ModelForm):
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={'tabindex': '-1', 'autocomplete': 'off'}))
    consent = forms.BooleanField(
        required=True,
        label=gettext_lazy("I agree that the club keeps these details to organise the tournament and shows the player's "
                 "name, school and results on the tournament pages."))

    class Meta:
        model = Registration
        fields = ['name', 'gender', 'birth_year', 'age_group', 'school', 'division', 'contact_name', 'contact', 'notes']
        widgets = {
            'gender': forms.RadioSelect,
            'school': forms.TextInput(attrs={'list': 'school-options', 'autocomplete': 'off'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['gender'] = forms.ChoiceField(
            label=_("Gender"), choices=[('K', _("Female")), ('M', _("Male"))], widget=forms.RadioSelect)
        year = (tournament.date.year if tournament else timezone.now().year)
        self.fields['birth_year'] = forms.TypedChoiceField(
            label=_("Year of birth"), required=False, coerce=int, empty_value=None,
            choices=[('', '—')] + [(y, y) for y in range(year - 4, year - 70, -1)])
        groups = list(DivisionCategory.objects.filter(owner=tournament.owner).values_list('name', flat=True))
        if groups:
            self.fields['age_group'] = forms.ChoiceField(
                label=_("Age group"), required=False, choices=[('', '—')] + [(g, g) for g in groups])
        else:
            del self.fields['age_group']
        divs = tournament.divisions.filter(discipline='single').order_by('schedule_priority', 'name')
        if divs.exists():
            self.fields['division'].queryset = divs
            self.fields['division'].label = _("Tournament category (if you know it)")
            self.fields['division'].empty_label = _("The club will decide")
        else:
            del self.fields['division']
        self.fields['school'].help_text = _("Leave it empty if the player does not represent any school or club.")
        self.fields['contact'].help_text = _("Only for the club, in case it has to contact you. Not shown in public.")
        self.fields['contact_name'].help_text = ''
        self.school_options = school_options(tournament.owner)

    def clean_name(self):
        return ' '.join(self.cleaned_data['name'].split())

    def clean_school(self):
        value = ' '.join(self.cleaned_data.get('school', '').split())
        for existing in self.school_options:
            if existing.lower() == value.lower():
                return existing
        return value


def _client_ip(request):
    return request.META.get('REMOTE_ADDR', '')


def public_register(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    _activate_club_language(request, tournament)
    if not tournament.registration_open:
        return render(request, 'tournaments/public/register.html', {'tournament': tournament, 'closed': True})
    done = request.GET.get('done') == '1'
    initial = request.session.get('reg_defaults', {})
    if request.method == 'POST':
        form = RegistrationForm(request.POST, tournament=tournament)
        if form.is_valid():
            if form.cleaned_data.get('website'):  # honeypot filled in: a bot
                return redirect(f"{request.path}?done=1")
            key = f"reg-ip:{_client_ip(request)}"
            count = cache.get(key, 0)
            if count >= MAX_PER_IP_PER_HOUR or tournament.registrations.filter(status='pending').count() >= MAX_PENDING:
                messages.error(request, _("Too many registrations right now. Please contact the club."))
            else:
                cache.set(key, count + 1, 3600)
                reg = form.save(commit=False)
                reg.tournament = tournament
                reg.save()
                request.session['reg_defaults'] = {
                    'school': reg.school, 'contact_name': reg.contact_name, 'contact': reg.contact}
                request.session['reg_last'] = reg.name
                return redirect(f"{request.path}?done=1")
    else:
        form = RegistrationForm(tournament=tournament, initial=initial)
    return render(request, 'tournaments/public/register.html', {
        'tournament': tournament,
        'form': form,
        'done': done,
        'last_name': request.session.get('reg_last', '') if done else '',
    })


# ---------------------------------------------------------------------------
# Club side
# ---------------------------------------------------------------------------

@login_required
def tournament_registrations(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    regs = list(tournament.registrations.select_related('division', 'player'))
    pending = [r for r in regs if r.status == 'pending']
    existing = {(p.name.lower(), p.gender): p for p in Player.objects.filter(owner=request.user)}
    for r in pending:
        r.existing_player = existing.get((r.name.lower(), r.gender))
    link = request.build_absolute_uri(f'/public/tournament/{tournament.pk}/inscripcio/')
    from django.conf import settings
    base = getattr(settings, 'PUBLIC_BASE_URL', '').rstrip('/')
    if base:
        link = f'{base}/public/tournament/{tournament.pk}/inscripcio/'
    from .court_access import qr_svg
    return render(request, 'tournaments/registrations.html', {
        'tournament': tournament,
        'pending': pending,
        'handled': [r for r in regs if r.status != 'pending'],
        'divisions': tournament.divisions.filter(discipline='single').order_by('schedule_priority', 'name'),
        'link': link,
        'qr': qr_svg(link),
    })


@login_required
@require_POST
def registration_toggle(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    tournament.registration_open = not tournament.registration_open
    tournament.save(update_fields=['registration_open'])
    if tournament.registration_open:
        messages.success(request, _("Online registration is open."))
    else:
        messages.success(request, _("Online registration is closed."))
    return redirect('tournament_registrations', pk=pk)


def accept_registration(reg, division=None):
    """Create (or reuse) the player and, when a category is given, enter them in it."""
    tournament = reg.tournament
    owner = tournament.owner
    player = (Player.objects.filter(owner=owner, name__iexact=reg.name, gender=reg.gender).first())
    age = tournament.date.year - reg.birth_year if reg.birth_year else None
    if player is None:
        player = Player.objects.create(owner=owner, name=reg.name, gender=reg.gender, age=age,
                                       division=reg.age_group, school=reg.school)
    else:
        changed = []
        for field, value in (('school', reg.school), ('division', reg.age_group), ('age', age)):
            if value and not getattr(player, field):
                setattr(player, field, value)
                changed.append(field)
        if changed:
            player.save(update_fields=changed)
    division = division or reg.division
    if division and division.discipline == 'single' and division.tournament_id == tournament.pk:
        team, _c = Team.objects.get_or_create(player1=player, player2=None, defaults={'name': player.name})
        division.teams.add(team)
    reg.player = player
    reg.division = division
    reg.status = 'accepted'
    reg.save(update_fields=['player', 'division', 'status'])
    return player


@login_required
@require_POST
def registration_accept(request, pk, reg_pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    reg = get_object_or_404(Registration, pk=reg_pk, tournament=tournament, status='pending')
    division = None
    if request.POST.get('division'):
        division = get_object_or_404(Division, pk=request.POST['division'], tournament=tournament)
    with transaction.atomic():
        accept_registration(reg, division)
    messages.success(request, _("%(name)s has been added.") % {'name': reg.name})
    return redirect('tournament_registrations', pk=pk)


@login_required
@require_POST
def registration_accept_all(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    n = 0
    with transaction.atomic():
        for reg in tournament.registrations.filter(status='pending').select_related('division', 'tournament__owner'):
            accept_registration(reg)
            n += 1
    messages.success(request, _("%(n)s registrations accepted.") % {'n': n})
    return redirect('tournament_registrations', pk=pk)


@login_required
@require_POST
def registration_reject(request, pk, reg_pk):
    tournament = get_object_or_404(Tournament, pk=pk, owner=request.user)
    reg = get_object_or_404(Registration, pk=reg_pk, tournament=tournament)
    if request.POST.get('undo') == '1':
        reg.status = 'pending'
    else:
        reg.status = 'rejected'
    reg.save(update_fields=['status'])
    return redirect('tournament_registrations', pk=pk)
