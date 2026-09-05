# PINNED TO BOOKWORM, not :slim. `python:3.12-slim` now resolves to Debian 13 (trixie),
# where the t64 ABI transition renamed the packages `playwright install --with-deps`
# expects (libasound2 -> libasound2t64, and friends). Playwright's supported-Linux list is
# Ubuntu/bookworm-shaped, so the browser build breaks on trixie -- and it breaks in the
# ONE image that has to fetch WAF-gated sources. Unpin only with a green browser build.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system -e ".[dev]"

# Chromium only where a source needs it; keeps the other images ~400MB smaller.
#
# NOTE: with browser fetches running inline in the orchestrator rather than on
# QUEUE_BROWSER (todo.md bug 14), the roles that need this are `api` and `worker` -- NOT
# the profile-gated `browserworker`, which no longer receives them (todo.md bug 15).
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then playwright install --with-deps chromium; fi

COPY alembic.ini ./
COPY alembic ./alembic

# The corpus the MOCK fetcher reads. Without it, PROVIDER_MODE=mock inside a container
# resolves every citation to a soft-404 -- which is NOT_FOUND, which is the only
# citation-level FAIL -- so a demo run reports real cases as fabricated. It is ~240kB and
# it is the difference between mock mode working and mock mode lying (todo.md bug 11).
COPY tests/corpus ./tests/corpus
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV ROLE=api
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
