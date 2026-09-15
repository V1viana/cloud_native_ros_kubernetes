import os
from celery import Celery

# For local development, use the base settings.
# Production deploys can override this with DJANGO_SETTINGS_MODULE in the environment.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings.base")

app = Celery("kuberos")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
