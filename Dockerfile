# syntax=docker/dockerfile:1
#
# Built on Docker Hardened Images (DHI): minimal, near-zero CVE base images from Docker, with an SBOM and
# signed SLSA Build Level 3 provenance. Free under Apache 2.0. Pulling needs a free Docker account:
#
#   docker login dhi.io
#   docker build -t root-cause-console .
#
# No Docker account? Build the hand-hardened Alpine image instead: docker build -f Dockerfile.alpine .
#
# Two stages, both Python 3.12 on the same Alpine release, so prebuilt packages match the runtime exactly.
# The -dev image has a shell, pip, and root, and is used only to install packages. The runtime image has
# no shell, no package manager, and no pip, so nothing needs to be removed from it afterwards.

ARG PYTHON_TAG=3.12-alpine3.24

FROM dhi.io/python:${PYTHON_TAG}-dev AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /build/requirements.txt
# Prebuilt packages only, so no compiler is ever needed.
RUN pip install --no-cache-dir --no-compile --only-binary=:all: --target /opt/packages -r /build/requirements.txt
# The console runs as user 10001, as in earlier releases, so existing data volumes stay writable.
# The runtime image has no shell to add the user with, so the account files are prepared here.
RUN echo "console:x:10001:10001::/nonexistent:/sbin/nologin" >> /etc/passwd \
 && echo "console:x:10001:" >> /etc/group \
 && mkdir /data

FROM dhi.io/python:${PYTHON_TAG}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/packages RCC_HOST=0.0.0.0 RCC_PORT=8000 RCC_DATA_DIR=/data
LABEL org.opencontainers.image.title="Root Cause Console" \
      org.opencontainers.image.description="Open-source, self-hosted AI SRE agent. Built on Docker Hardened Images." \
      org.opencontainers.image.source="https://github.com/AymaneK24/SRE-AI-Agent" \
      org.opencontainers.image.url="https://rootcauseconsole.online" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Aymane KENBOUCH"

COPY --from=build /etc/passwd /etc/group /etc/
COPY --from=build --chown=10001:10001 /data /data
# Owned by root, so the running app can't change its own code.
COPY --from=build /opt/packages /opt/packages
COPY app /srv/app

USER 10001:10001
WORKDIR /srv
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
ENTRYPOINT ["python", "-m", "app"]
