# kvm-bridge in a container. Run it with host networking (`--network host`):
# the bridge binds specific addresses (loopback + the tailnet IP), and WebRTC's
# ICE needs the host's own interfaces to reach the JetKVM.
FROM ghcr.io/astral-sh/uv:0.8.17-python3.13-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY bridge ./bridge
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --system --uid 8379 --home-dir /data bridge \
 && mkdir -p /data && chown bridge /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH BRIDGE_HOME=/data PYTHONUNBUFFERED=1
USER bridge
VOLUME /data
ENTRYPOINT ["bridge"]
CMD ["serve"]
