# mini_cc/sandbox/Dockerfile
# Base image: locally available python:3.10-slim in WSL2 Ubuntu-24.04
# (avoids network pull during build; 3.12-slim not present locally)
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
