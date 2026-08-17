ARG BASE_IMAGE=python:3.10-slim
FROM ${BASE_IMAGE}

ARG DEBIAN_MIRROR=""
ARG DEBIAN_SECURITY_MIRROR=""
ARG PIP_INDEX_URL="https://pypi.org/simple"

WORKDIR /app

RUN set -eu; \
    if [ -n "$DEBIAN_MIRROR" ] || [ -n "$DEBIAN_SECURITY_MIRROR" ]; then \
        if [ -z "$DEBIAN_MIRROR" ] || [ -z "$DEBIAN_SECURITY_MIRROR" ]; then \
            echo "DEBIAN_MIRROR and DEBIAN_SECURITY_MIRROR must be set together" >&2; \
            exit 1; \
        fi; \
        for sources_file in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "$sources_file" ] || continue; \
            sed -i \
                -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
                -e "s|https://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
                -e "s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
                -e "s|https://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
                -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
                -e "s|https://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
                "$sources_file"; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends libimage-exiftool-perl; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN test -n "$PIP_INDEX_URL" \
    && pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY . .
RUN mkdir -p /app/data/output /app/logs

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.server.run_server"]
