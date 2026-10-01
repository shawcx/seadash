# Linux with g++-14 and the libraries seadash links (see tools/linux/check.sh).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++-14 python3 python3-venv python3-pip rsync ca-certificates \
        zlib1g-dev libpcre2-dev libssl-dev libbz2-dev liblzma-dev libsqlite3-dev \
        tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv && /venv/bin/pip install -q pytest
WORKDIR /work
