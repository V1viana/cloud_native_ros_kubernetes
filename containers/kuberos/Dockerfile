FROM python:3.9-bullseye@sha256:3dcaf39bbe2d6d8031a4a7ce68a20f2d494419aca18ec064f9e82e146e97a0ed

COPY integrations/kuberos/requirements.txt /tmp/requirements.txt
RUN pip --no-cache-dir install -r /tmp/requirements.txt

COPY integrations/kuberos/kuberos /kuberos
WORKDIR /kuberos

ENV DJANGO_SETTINGS_MODULE=settings.base
EXPOSE 8000
# License texts and attributions travel with the image (THIRD_PARTY_NOTICES).
COPY LICENSE THIRD_PARTY_NOTICES /usr/share/licenses/cloud-native-ros/
COPY integrations/kuberos/LICENSE /usr/share/licenses/cloud-native-ros/kuberos/LICENSE

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", \
     "--timeout", "120", "settings.wsgi"]
