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

RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY src ./src
COPY schema ./schema
# Install with web + import + postgres + scrape extras (FastAPI, MCP, pypdf,
# psycopg, BeautifulSoup — bs4 is needed by the EUR-Lex HTML and BWB parsers).
RUN uv pip install --system ".[web,import,postgres,scrape]"

# Bundle the built UI; the API serves it when RAGLEX_FRONTEND_DIST points here.
COPY --from=ui /ui/dist /app/frontend/dist
ENV RAGLEX_FRONTEND_DIST=/app/frontend/dist

ENV RAGLEX_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000 8001
CMD ["raglex", "serve", "--host", "0.0.0.0", "--port", "8000"]
