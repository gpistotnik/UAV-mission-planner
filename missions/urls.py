from django.urls import path

from . import views

app_name = "missions"

urlpatterns = [
    path("", views.mission_list, name="list"),
    path("nova/", views.mission_planner, name="planner_new"),
    path("<int:pk>/", views.mission_detail, name="detail"),
    path("<int:pk>/urejanje/", views.mission_planner, name="planner_edit"),
    path("api/", views.mission_api_save, name="api_create"),
    path("api/<int:pk>/", views.mission_api_save, name="api_save"),
    path("api/<int:pk>/get/", views.mission_api_get, name="api_get"),
]
