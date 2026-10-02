# Fleet Operator is a plain Kubernetes controller (Kopf + the official
# kubernetes client) -- unlike the other containers/ images, it does not
# link against ROS 2, so it does not start from a ros:* base image.
FROM python:3.11-slim

WORKDIR /operator
COPY operator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY operator/fleet_operator ./fleet_operator
# Real image catalog, so ROSModuleController resolves the same
# control-plane/event-detector/kuberos bundle references the rest of the
# project already builds -- see k8s_workloads.py.
COPY config/project_images.json ./project_images.json
ENV PROJECT_IMAGES_PATH=/operator/project_images.json
# `kopf` is a console-script entrypoint, not `python -m kopf`: Python does
# not add the working directory to sys.path for it the way it would for a
# plain script invocation, so fleet_operator was never importable without
# this -- found live, the local venv runs always set PYTHONPATH by hand.
ENV PYTHONPATH=/operator

CMD ["kopf", "run", "--standalone", "-m", "fleet_operator.handlers"]
