from django.urls import path, include

from .urls import v1_urls

app_name = 'main'

urlpatterns = [
    path('v1/', include(v1_urls)),
]