# Build from the repository root. No credentials or dataset files enter the image.
FROM node:22-bookworm AS base
ENV DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_MIRROR=http://deb.debian.org
RUN sed -i "s#http://deb.debian.org#$DEBIAN_MIRROR#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git curl ca-certificates bash build-essential \
    ffmpeg poppler-utils tesseract-ocr tesseract-ocr-chi-sim libreoffice pandoc \
    texlive-xetex texlive-lang-chinese texlive-latex-extra texlive-fonts-recommended \
    fonts-dejavu fonts-noto-cjk lmodern jq ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/eval
ENV PATH="/opt/eval/bin:${PATH}"
COPY docker/requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir -r /opt/requirements.txt
RUN mkdir -p /tmp_workspace/results /root/.openclaw/agents/main/agent
RUN printf '%s\n' 'export PATH=/opt/eval/bin:$PATH' > /etc/profile.d/xperiencebench-python.sh
WORKDIR /tmp_workspace
CMD ["/bin/bash", "-c", "tail -f /dev/null"]

FROM base AS codex-cli
ARG CODEX_VERSION=0.121.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" && codex --version

FROM base AS openclaw-cli
ARG OPENCLAW_VERSION=2026.3.11
RUN npm install -g "openclaw@${OPENCLAW_VERSION}" && openclaw --version
COPY docker/openclaw.json /root/.openclaw/openclaw.json

# Keep the original system, Python and fixed CLI layers reusable. Only task
# dependencies belong in this shared extension; system Chromium avoids a
# separate Playwright browser download.
FROM base AS task-runtime
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
COPY docker/requirements.tasks.txt /opt/requirements.tasks.txt
RUN pip install --no-cache-dir -c /opt/requirements.txt -r /opt/requirements.tasks.txt \
    && pip check
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

FROM task-runtime AS codex
COPY --from=codex-cli /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && codex --version

FROM task-runtime AS openclaw
COPY --from=openclaw-cli /usr/local/lib/node_modules/openclaw /usr/local/lib/node_modules/openclaw
RUN ln -s ../lib/node_modules/openclaw/openclaw.mjs /usr/local/bin/openclaw \
    && openclaw --version
COPY docker/openclaw.json /root/.openclaw/openclaw.json
