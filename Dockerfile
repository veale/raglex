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

# tesseract: OCR fallback for scanned PDFs. Belgian court scans need Dutch + French;
# some carry only a tiny born-digital cover layer, so those adapters force a full pass.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-nld \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# ── Layer order is the build's performance contract ───────────────────────────
# Everything from here to `COPY src` depends ONLY on pyproject.toml + uv.lock, so an
# ordinary code change reuses all of it. That is not how this was written: `COPY src`
# used to sit ABOVE the dependency install, which put the app's own source in the cache
# key for every layer below it — so a one-line edit to a Python file re-ran the
# dependency install, the Playwright system deps AND the 1.3 GB Camoufox fetch. Measured
# on build 5c4dbb8 (a source-only change), those three ran for 7.6s + 16.7s + 17.3s on
# native amd64 and 31.2s + 142.2s + 108.9s emulated, every single push. The comment
# below already claimed the browser was "fetched once and cached across app-only
# rebuilds"; the intent was right and the ordering silently defeated it.
COPY pyproject.toml uv.lock README.md ./

# Install with web + import + postgres + scrape + ocr + bulk extras (FastAPI, MCP, pypdf,
# psycopg, BeautifulSoup — bs4 is needed by the EUR-Lex HTML and BWB parsers).
#
# FROM THE LOCKFILE, not a fresh resolve. Resolving at build time meant the image took
# whatever each dependency had released that morning: mcp 2.0 landed, moved FastMCP out of
# `mcp.server.fastmcp`, and the very next build produced an image whose API crash-looped on
# import — from a commit that changed nothing about MCP. The lock is what the tests run
# against, so this makes the image the same thing. Updating a dependency now means updating
# uv.lock, deliberately, in a commit.
#
# `--no-emit-project` is what lets this run before the source exists: it exports the
# THIRD-PARTY requirements only, and needs nothing but the manifest and the lock.
RUN uv export --frozen --no-emit-project \
        --extra web --extra import --extra postgres --extra scrape --extra ocr --extra bulk \
        --extra browser \
        -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt

# The browser itself (~1.3 GB), in its own layer so it is fetched once and cached across
# app-only rebuilds — which, with `COPY src` now below it, is finally true. It is what
# makes a Cloudflare-walled committee PDF readable at all: every plain request for one
# answers 403, and so does an XHR from a browser that has already cleared the challenge —
# only a real navigation returns the file.
#
# Its ANTI-FINGERPRINT PATCHES ARE IN THE BINARY, not the Python package, so this layer
# is where "current anti-bot technology" actually lives. The weekly dependency workflow
# (.github/workflows/update-antibot.yml) bumps the pins and rebuilds, which re-fetches
# this — that, and not an unpinned install, is how it stays current. Note that it stays
# current for exactly the right reason: the bump lands in uv.lock, which is in the cache
# key for this layer, so a pin change still re-fetches while a code change does not.
#
# `|| true` on the deps step only: a missing optional font package must not fail the
# build, but a missing BROWSER must, or the image silently ships unable to read Lords
# reports and says so one skipped document at a time.
RUN python -m playwright install-deps firefox || true
RUN python -m camoufox fetch \
    && python -c "import camoufox.sync_api" \
    && du -sh /root/.cache/camoufox

# ── From here down is the only part a code change rebuilds ────────────────────
COPY src ./src
COPY schema ./schema
# The project itself, without deps: they are already installed above from the same lock,
# so resolving again here could only disagree with it.
RUN uv pip install --system --no-deps .

# Bundle the built UI; the API serves it when RAGLEX_FRONTEND_DIST points here.
COPY --from=ui /ui/dist /app/frontend/dist
ENV RAGLEX_FRONTEND_DIST=/app/frontend/dist

ENV RAGLEX_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000 8001
CMD ["raglex", "serve", "--host", "0.0.0.0", "--port", "8000"]
