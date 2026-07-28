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
        -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt \
    && uv pip install --system --no-deps .

# Bundle the built UI; the API serves it when RAGLEX_FRONTEND_DIST points here.
COPY --from=ui /ui/dist /app/frontend/dist
ENV RAGLEX_FRONTEND_DIST=/app/frontend/dist

ENV RAGLEX_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000 8001
CMD ["raglex", "serve", "--host", "0.0.0.0", "--port", "8000"]
