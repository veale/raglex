# RagLex — API + MCP server image, with the React UI built in and served by the
# API at the same origin. `docker compose up` then gives the whole app on :8000.

# 1. Build the React UI.
FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build           # → /ui/dist

# 2. Python app image.
FROM python:3.12-slim
WORKDIR /app

# tesseract: OCR fallback for scanned PDFs (the EDPB one-stop-shop register holds
# decision scans with no text layer). eng only — the register PDFs are English.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY schema ./schema
# Install with web + import + postgres + scrape + ocr + bulk extras (FastAPI, MCP, pypdf,
# psycopg, BeautifulSoup — bs4 is needed by the EUR-Lex HTML and BWB parsers).
#
# FROM THE LOCKFILE, not a fresh resolve. Resolving at build time meant the image took
# whatever each dependency had released that morning: mcp 2.0 landed, moved FastMCP out of
# `mcp.server.fastmcp`, and the very next build produced an image whose API crash-looped on
# import — from a commit that changed nothing about MCP. The lock is what the tests run
# against, so this makes the image the same thing. Updating a dependency now means updating
# uv.lock, deliberately, in a commit.
RUN uv export --frozen --no-emit-project \
        --extra web --extra import --extra postgres --extra scrape --extra ocr --extra bulk \
        --extra browser \
        -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt \
    && uv pip install --system --no-deps .

# The browser itself (~1.3 GB), in its own layer so it is fetched once and cached across
# app-only rebuilds. It is what makes a Cloudflare-walled committee PDF readable at all:
# every plain request for one answers 403, and so does an XHR from a browser that has
# already cleared the challenge — only a real navigation returns the file.
#
# Its ANTI-FINGERPRINT PATCHES ARE IN THE BINARY, not the Python package, so this layer
# is where "current anti-bot technology" actually lives. The weekly dependency workflow
# (.github/workflows/update-antibot.yml) bumps the pins and rebuilds, which re-fetches
# this — that, and not an unpinned install, is how it stays current.
#
# `|| true` on the deps step only: a missing optional font package must not fail the
# build, but a missing BROWSER must, or the image silently ships unable to read Lords
# reports and says so one skipped document at a time.
RUN python -m playwright install-deps firefox || true
RUN python -m camoufox fetch \
    && python -c "import camoufox.sync_api" \
    && du -sh /root/.cache/camoufox

# Bundle the built UI; the API serves it when RAGLEX_FRONTEND_DIST points here.
COPY --from=ui /ui/dist /app/frontend/dist
ENV RAGLEX_FRONTEND_DIST=/app/frontend/dist

ENV RAGLEX_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000 8001
CMD ["raglex", "serve", "--host", "0.0.0.0", "--port", "8000"]
