FROM python:3.9-bullseye

COPY integrations/kuberos/requirements.txt /tmp/requirements.txt
RUN pip --no-cache-dir install -r /tmp/requirements.txt

COPY integrations/kuberos/kuberos /kuberos
WORKDIR /kuberos

ENV DJANGO_SETTINGS_MODULE=settings.base
EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", \
     "--timeout", "120", "settings.wsgi"]
