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

# Security fixes. A failure here must stop the build, so nothing in this step is allowed to fail quietly.
RUN apk upgrade --no-cache
# Fixes Alpine has released on edge but not yet copied to the stable release this image is on. The minimum versions
# make the build fail if they aren't available. Remove a line once the stable release has the same fix.
#   libcrypto3, libssl3 >= 3.5.9-r1  OpenSSL: CVE-2026-88822, -88823, -95691, -95692, -107496
#   musl >= 1.2.6-r6                 CVE-2026-107891 (Alpine 3.24 lists it fixed in 1.2.6-r3; edge's record says r6)
#   sqlite-libs >= 3.54.1-r0         CVE-2026-119586; used by Python's sqlite3 when no PostgreSQL is configured
ARG ALPINE_EDGE=https://dl-cdn.alpinelinux.org/alpine/edge/main
RUN apk add --no-cache --upgrade --repository="$ALPINE_EDGE" \
      'libcrypto3>=3.5.9-r1' 'libssl3>=3.5.9-r1' 'musl>=1.2.6-r6' 'sqlite-libs>=3.54.1-r0' \
 && python -c "import ssl, sqlite3; print(ssl.OPENSSL_VERSION, '| SQLite', sqlite3.sqlite_version)"
# Remove what the running app has no use for. pip alone accounts for several findings in a scan.
RUN python -m pip uninstall -y --quiet pip setuptools wheel || true
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
