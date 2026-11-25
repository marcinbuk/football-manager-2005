FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      wine \
      wine32 \
      xvfb \
      x11vnc \
      openbox \
      novnc \
      websockify \
      nginx \
      python3 \
      fonts-liberation \
      fonts-dejavu-core \
      procps \
      git \
      curl \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 gamer && \
    useradd -u 1000 -g 1000 -m -s /bin/bash gamer && \
    mkdir -p /app/web /app/game /data/saves /opt/wine-template /tmp/fm-sessions /home/gamer/.config/openbox && \
    chown -R 1000:1000 /app /data/saves /opt/wine-template /tmp/fm-sessions /home/gamer

COPY --chown=1000:1000 nginx.conf /app/nginx.conf
COPY --chown=1000:1000 start.sh /app/start.sh
COPY --chown=1000:1000 session_manager.py /app/session_manager.py
COPY --chown=1000:1000 index.html /app/web/index.html
COPY --chown=1000:1000 openbox-rc.xml /home/gamer/.config/openbox/rc.xml

RUN sed -i 's/\r$//' /app/start.sh /app/nginx.conf /app/session_manager.py && \
    chmod +x /app/start.sh

USER gamer
WORKDIR /app

ENV HOME=/home/gamer \
    GAME_DIR=/app/game \
    SAVES_DIR=/data/saves \
    IDLE_TIMEOUT_SECONDS=1800 \
    MAX_SESSIONS=0

RUN WINEARCH=win32 WINEPREFIX=/opt/wine-template WINEDEBUG=-all \
    WINEDLLOVERRIDES="msvcr71=n,b;mscoree,mshtml=" \
    bash -c 'wineboot --init && wineserver -w && \
    wine reg add "HKCU\\Software\\Wine" /v Version /t REG_SZ /d winxp /f && \
    wine reg add "HKCU\\Software\\Wine\\Drivers" /v Audio /t REG_SZ /d "" /f && \
    wineserver -w'

EXPOSE 7860

CMD ["/app/start.sh"]
