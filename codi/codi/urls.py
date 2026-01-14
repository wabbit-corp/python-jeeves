"""
codi URL Configuration
"""

from django.urls import include, path

urlpatterns = [path("", include("client.urls.urls")), path("api/", include("api.urls.urls"))]
