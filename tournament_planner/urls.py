"""
URL configuration for tournament_planner project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.template.response import TemplateResponse
from tournaments.models import Tournament
from tournaments.public_views import public_landing, public_tournament, public_schedule, public_player
from tournaments import court_views
from tournaments.registration_views import public_register
from tournaments import connection_views

# Admin titles in the club's language (Django's default is "Django administration").
from django.utils.translation import gettext_lazy as _
admin.site.site_header = _("Club administration")
admin.site.site_title = _("Club Bàdminton Igualada")
admin.site.index_title = _("Advanced administration")

import datetime
import os

# Move the admin off the well-known /admin/ path to reduce brute-force noise.
# Configure via ADMIN_URL env var (must end in '/' and NOT start with '/'),
# e.g. ADMIN_URL=staff-7x2/.
_admin_url = os.environ.get('ADMIN_URL', 'admin/').lstrip('/')
if not _admin_url.endswith('/'):
    _admin_url += '/'

@login_required
def home(request):
    """Render dashboard with upcoming and recent tournaments.

    Args:
        request: Django HTTP request.

    Returns:
        HttpResponse: Rendered dashboard page.
    """
    upcoming = Tournament.objects.filter(owner=request.user, date__gte=datetime.date.today()).order_by('date')[:3]
    recent = Tournament.objects.filter(owner=request.user, date__lt=datetime.date.today()).order_by('-date')[:3]
    return render(request, 'home.html', {'upcoming': upcoming, 'recent': recent})

def service_worker(request):
    return TemplateResponse(request, 'pwa/sw.js', content_type='application/javascript')


urlpatterns = [
    path('i18n/', include('django.conf.urls.i18n')),
    path('sw.js', service_worker, name='service_worker'),
    path('', public_landing, name='home'),
    path('dashboard/', home, name='admin_home'),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path(_admin_url, admin.site.urls),
    path('players/', include('players.urls')),
    path('tournaments/', include('tournaments.urls')),
    # ── Public / anonymous viewer ──────────────────────────────────────────
    path('public/', public_landing, name='public_landing'),
    path('public/tournament/<int:pk>/', public_tournament, name='public_tournament'),
    path('public/tournament/<int:pk>/jugador/<int:player_pk>/', public_player, name='public_player'),
    path('public/tournament/<int:pk>/inscripcio/', public_register, name='public_register'),
    path('prova-connexio/', connection_views.connection_test, name='connection_test'),
    path('prova-connexio/ping/', connection_views.connection_ping, name='connection_ping'),
    path('prova-connexio/dades/', connection_views.connection_payload, name='connection_payload'),
    # Court referee pages (signed link per court, no login)
    path('pista/<int:tournament_pk>/<int:court>/<str:sig>/', court_views.court_panel, name='court_panel'),
    path('pista/<int:tournament_pk>/<int:court>/<str:sig>/start/<int:match_pk>/', court_views.court_start, name='court_start'),
    path('pista/<int:tournament_pk>/<int:court>/<str:sig>/result/<int:match_pk>/', court_views.court_result, name='court_result'),
    path('pista/<int:tournament_pk>/<int:court>/<str:sig>/cancel/<int:match_pk>/', court_views.court_cancel, name='court_cancel'),
    path('pista/<int:tournament_pk>/<int:court>/<str:sig>/walkover/<int:match_pk>/', court_views.court_walkover, name='court_walkover'),
    path('public/tournament/<int:pk>/spilleplan/', public_schedule, name='public_schedule'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
