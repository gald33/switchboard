# Switchboard hub — one process, one SQLite file.
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist

FROM python:3.12-slim

# The hub holds no source and no credentials, but there is no reason to run
# it as root either.
RUN useradd --create-home --uid 10001 switchboard \
 && mkdir -p /data && chown switchboard:switchboard /data

COPY --from=build /dist/*.whl /tmp/
# The wheel path is resolved into a variable first: `/tmp/*.whl[server]` would
# be read by the shell as a glob with a [...] character class, which matches
# nothing and gets passed to pip verbatim.
RUN wheel="$(ls /tmp/*.whl)" \
 && pip install --no-cache-dir "${wheel}[server]" \
 && rm /tmp/*.whl

USER switchboard
WORKDIR /data

ENV SWITCHBOARD_DB=/data/switchboard.db \
    PYTHONUNBUFFERED=1

EXPOSE 8787
VOLUME ["/data"]

# Kept to one physical line: a backslash-continued CMD inside a quoted Python
# one-liner is easy to break silently, and a healthcheck that always fails
# takes the container down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"]

ENTRYPOINT ["switchboard", "serve", "--host", "0.0.0.0", "--port", "8787"]
