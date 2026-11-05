# RAHBAR api image. Build context: api/   (docker build -t rahbar-api:TAG api)
# Stage 1 installs the pinned dependencies into a virtualenv; stage 2 copies only that venv
# and the app, so no pip cache or compiler ends up in the runtime image.
FROM python:3.12.11-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.11-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Take the fixed versions of the base image's own packages (openssl, gnutls, krb5 ...).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app /app/app
# Non-root, fixed uid: matches runAsUser in the Deployment. The filesystem is read-only at
# run time; the app writes nothing.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
