FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
        libavdevice-dev libavfilter-dev libopus-dev libvpx-dev libsrtp2-dev pkg-config gcc \
    && rm -rf /var/lib/apt/lists/*

# MediaMTX: single static binary, no runtime deps. Pin the version tested against this design.
ARG MEDIAMTX_VERSION=v1.19.2
RUN curl -sL -o /tmp/mediamtx.tar.gz \
      https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz \
    && tar xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx \
    && rm /tmp/mediamtx.tar.gz

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mediamtx.yml /app/mediamtx.yml
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
COPY bridge/ /app/bridge/

# No component here needs root: MediaMTX's RTSP/RTP/RTCP ports (8554/8000/8001) and
# the control endpoint (8199) are all >1024, and everything this process writes to
# lives under /app or the bind-mounted /shared (host-side ownership must match this
# UID -- see README's "non-root bridge user" note for the corresponding host chown).
RUN groupadd -r bridge --gid 1500 && useradd -r -g bridge --uid 1500 bridge \
    && chown -R bridge:bridge /app
USER bridge

ENTRYPOINT ["/app/entrypoint.sh"]
