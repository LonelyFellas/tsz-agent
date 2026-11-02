FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.13-slim
WORKDIR /app
COPY --from=build /app /app
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home agent \
    && install -d -m 0700 -o agent -g agent /var/lib/tsz-agent/chatgpt
USER agent
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TSZ_AGENT_CHATGPT_AUTH_DIR=/var/lib/tsz-agent/chatgpt \
    TSZ_AGENT_CHATGPT_LOGIN_MODE=ssh
EXPOSE 8390
CMD ["uvicorn", "tsz_agent.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8390"]
