FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 sandbox && useradd --uid 1000 --gid 1000 --create-home sandbox \
    && mkdir -p /workspace /opt/bub /sys/fs/cgroup /dev/pts /dev/shm \
    && chown 1000:1000 /workspace
COPY agent.py /opt/bub/agent.py
RUN chmod 755 /opt/bub /opt/bub/agent.py
