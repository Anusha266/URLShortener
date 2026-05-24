from django.urls import path

from . import views

# App-level URL routes.
urlpatterns = [
    path('', views.home, name='home'),
    path('api/shorten', views.shorten, name='shorten'),
    path('api/stats/<slug:short_code>', views.stats_view, name='stats'),
    # Catch-all short code at the root. Must come last so it doesn't
    # shadow the routes above. Restrict to base62 chars only.
    path('<slug:short_code>', views.redirect_view, name='redirect'),
]
