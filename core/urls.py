from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="home"),
    path("nadzor/", views.dashboard, name="dashboard"),
]
