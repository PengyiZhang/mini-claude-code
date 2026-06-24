# mini_cc/sandbox/Dockerfile
# Default base: python:3.10-slim (small, broadly cached). Works on any
# Debian release the python image targets — Bookworm / Trixie / Bullseye.
# If you need to swap bases (e.g. an older Bullseye image whose apt repos
# have been retired), change FROM below and add a sources swap before
# the apt-get line.
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git git-lfs ca-certificates curl unzip \
        ripgrep fd-find file less vim-tiny \
        build-essential \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 agent
USER agent
WORKDIR /workspaces
CMD ["sleep", "infinity"]
