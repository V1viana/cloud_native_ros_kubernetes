"""Celery tasks discovered for the main Django application."""

from .deployment_update import apply_deployment_update


__all__ = ['apply_deployment_update']
