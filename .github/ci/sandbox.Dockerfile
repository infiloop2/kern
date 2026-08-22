# CI sandbox for kern-host tests. The image is built with network
# access, but tests always run in it with --network none (run-in-sandbox.sh),
# so code arriving through a pull request has no outbound network path.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

ARG PGVECTOR_VERSION=0.8.6
ARG PGVECTOR_SHA256=10bf9938906e5d643bbc4a7eea104b6f57ba4898e5b76b20e60484ea1d5a7f8f

# The runtime is Python standard library only; the tests additionally need
# openssl (proxy certificate tests), bash (rendered script checks), rsync
# (sandbox workspace copy), a PostgreSQL server (admin-state tests start a
# scratch cluster on a Unix socket, so --network none still holds), and
# libnss-wrapper (initdb needs a passwd entry for the arbitrary uid the
# sandbox runs as). The admin UI browser smoke needs Playwright and Chromium
# installed while the image still has build-time network access.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    libnss-wrapper \
    openssl \
    postgresql \
    postgresql-server-dev-14 \
    python3.11 \
    python3.11-venv \
    rsync \
  && curl -fsSLo /tmp/pgvector.tar.gz \
       "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
  && echo "${PGVECTOR_SHA256}  /tmp/pgvector.tar.gz" | sha256sum --check --status \
  && mkdir /tmp/pgvector \
  && tar -xzf /tmp/pgvector.tar.gz -C /tmp/pgvector --strip-components=1 \
  && make -C /tmp/pgvector \
  && make -C /tmp/pgvector install \
  && rm -rf /tmp/pgvector /tmp/pgvector.tar.gz \
  && apt-get purge -y build-essential postgresql-server-dev-14 \
  && apt-get autoremove -y --purge \
  && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
  && rm -rf /var/lib/apt/lists/*

COPY tests/requirements.txt /tmp/test-requirements.txt

RUN python3.11 -m venv /opt/kern-ci-venv \
  && /opt/kern-ci-venv/bin/python -m pip install --upgrade pip \
  && /opt/kern-ci-venv/bin/python -m pip install -r /tmp/test-requirements.txt \
  && /opt/kern-ci-venv/bin/python -m mypy --version \
  && /opt/kern-ci-venv/bin/python -m pyright --version \
  && /opt/kern-ci-venv/bin/python -m playwright install --with-deps chromium webkit \
  && rm -f /tmp/test-requirements.txt

ENV PATH="/opt/kern-ci-venv/bin:${PATH}"
