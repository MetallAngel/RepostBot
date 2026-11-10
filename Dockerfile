FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends chromium ffmpeg tesseract-ocr tesseract-ocr-rus ca-certificates fonts-liberation curl xvfb && rm -rf /var/lib/apt/lists/*
# Twitch lists Google Chrome as a supported browser. Its official Linux build
# is amd64 only; retain Chromium for arm64 and the other page parsers.
RUN --mount=type=secret,id=proxy_ca \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
        if [ -s /run/secrets/proxy_ca ]; then export CURL_CA_BUNDLE=/run/secrets/proxy_ca; fi; \
        curl --fail --location --retry 2 --max-time 180 https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/scout-chrome.deb && \
        apt-get update && apt-get install -y --no-install-recommends /tmp/scout-chrome.deb && \
        rm -f /tmp/scout-chrome.deb && rm -rf /var/lib/apt/lists/*; \
    fi
RUN --mount=type=secret,id=proxy_ca \
    if [ -s /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir 'PyJWT[crypto]>=2.10.1,<3' 'Telethon>=1.45,<2' 'pypdf>=5,<7' 'playwright>=1.49,<2' 'websocket-client>=1.8,<2'
COPY repostbot /app/repostbot
COPY version.json /app/version.json
COPY start_container.sh /app/start_container.sh
ENV PYTHONUNBUFFERED=1 WEB_HOST=0.0.0.0 DATA_DIR=/data
EXPOSE 8787
CMD ["sh", "/app/start_container.sh"]
