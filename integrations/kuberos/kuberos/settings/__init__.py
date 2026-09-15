from .celery import app as celery_app

app = celery_app

__all__ = ("celery_app", "app")
