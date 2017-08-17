
from django.conf.urls import url

from . import views

app_name = 'django_airavata_groups'
urlpatterns = [
    url(r'^$', views.groups_manage, name="manage"),
    url(r'^create/', views.groups_create, name='create'),
    url(r'^view/', views.view_group, name='view'),
    url(r'^add/', views.add_members, name='add'),
    url(r'^remove/', views.remove_members, name='remove'),
    url(r'^delete/', views.delete_group, name='delete'),
    url(r'^leave/', views.leave_group, name='leave'),
]
