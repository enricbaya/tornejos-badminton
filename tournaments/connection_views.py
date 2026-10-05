"""Venue connection test: open it on the phones and the judge-table laptop at the venue."""
import os

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

_PAYLOAD = None


def connection_test(request):
    return render(request, 'tournaments/connection_test.html')


@never_cache
def connection_ping(request):
    return JsonResponse({'ok': True})


@never_cache
def connection_payload(request):
    """256 KB of random bytes (not compressible) to measure download speed."""
    global _PAYLOAD
    if _PAYLOAD is None:
        _PAYLOAD = os.urandom(256 * 1024)
    resp = HttpResponse(_PAYLOAD, content_type='application/octet-stream')
    return resp
