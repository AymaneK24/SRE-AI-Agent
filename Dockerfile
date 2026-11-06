# Two stages. Tools used to build never reach the image that runs.
#
# Alpine has far fewer packages than Debian, and its packages get security fixes quickly. Rebuild regularly
# with --pull, because `apk upgrade` below only picks up fixes at build time.

FROM python:3.12-alpine AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /build/requirements.txt
# Prebuilt packages only, so no compiler is ever needed.
RUN pip install --no-cache-dir --no-compile --only-binary=:all: --target /opt/packages -r /build/requirements.txt

FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/packages RCC_HOST=0.0.0.0 RCC_PORT=8000 RCC_DATA_DIR=/data

# Security fixes, then remove what the running app has no use for. pip alone accounts for several findings in a scan.
RUN apk upgrade --no-cache \
 && python -m pip uninstall -y --quiet pip setuptools wheel || true
RUN rm -rf /usr/local/bin/pip* /usr/local/bin/idle* /usr/local/bin/pydoc* /usr/local/bin/2to3* \
           /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/idlelib \
           /usr/local/lib/python3.12/lib2to3 /usr/local/lib/python3.12/tkinter /usr/local/lib/python3.12/test \
 && echo "console:x:10001:10001::/nonexistent:/sbin/nologin" >> /etc/passwd \
 && echo "console:x:10001:" >> /etc/group \
 && mkdir /data && chown 10001:10001 /data

# Owned by root, so the running app can't change its own code.
COPY --from=build /opt/packages /opt/packages
COPY app /srv/app

# Last, remove the package manager and the shell. After this, nobody can open a shell in the container,
# so `python -m app` is the only way in. Package records stay, so security scanners still see what's here.
COPY deploy/harden-image.py /tmp/harden-image.py
RUN ["python", "/tmp/harden-image.py"]

USER 10001:10001
WORKDIR /srv
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
ENTRYPOINT ["python", "-m", "app"]
