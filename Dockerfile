# The image is built from the wheel (the same file that `uv tool install penguwatch` or pip installs), in two stages.
# The runtime image has no source tree, no tests and no build tools.
#
#   podman-compose build / scripts/deploy.sh   builds the wheel from the source tree (stage "wheel-built").
#   CI and releases                            put an already-built wheel in dist/ and build with
#                                              --build-arg WHEEL_STAGE=wheel-prebuilt, so the image holds
#                                              exactly the file that was tested and attached to the release.
#
# python:3.12-slim, pinned by digest (refresh the digest when updating the base image)
ARG PYTHON_IMAGE=python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ARG WHEEL_STAGE=wheel-built

# ---- build: the wheel, with the hash-locked build backend; no build isolation, no download at build time ----
FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r requirements-build.txt
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
# The wheel is what the image installs; the sdist (same backend, PEP 517 hook called directly: no extra tool) is for
# the release.
RUN pip wheel --no-deps --no-build-isolation -w /dist . \
    && python -c "from setuptools import build_meta; build_meta.build_sdist('/dist')"

FROM scratch AS wheel-built
COPY --from=build /dist/ /

# A wheel built elsewhere (CI): any penguwatch-*.whl in dist/ of the build context.
FROM scratch AS wheel-prebuilt
COPY dist/penguwatch-*.whl /

FROM ${WHEEL_STAGE} AS wheel

# ---- runtime ----
FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Security updates of the base image
RUN apt-get update && apt-get -y --no-install-recommends upgrade && rm -rf /var/lib/apt/lists/*

# Every package is installed from a hash-locked file (see docs/supply-chain.md), then the wheel itself.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r requirements.txt
# COPY, not a bind mount: a COPY layer's cache key is the wheel's content, while a RUN --mount=type=bind layer is
# cached on the command text alone, so Podman could reuse a layer holding another tree's wheel.
COPY --from=wheel / /tmp/wheel/
RUN pip install --no-cache-dir --no-deps /tmp/wheel/penguwatch-*.whl \
    && rm -rf /tmp/wheel \
    && pip uninstall -y setuptools \
    && python -m compileall -q "$(python -c 'import penguwatch, os; print(os.path.dirname(penguwatch.__file__))')"

# The image carries only the example config; compose mounts your own config.yaml over it (nothing private is baked in).
COPY config.example.yaml ./config.yaml

# SQLite database, SIMKL token and outbox live here (mount a named volume).
# Runs as an unprivileged user; an existing root-owned volume needs a one-time `chown -R 10001:0`.
RUN useradd --system --uid 10001 --gid 0 --no-create-home app \
    && mkdir -p /app/data && chown -R 10001:0 /app/data && chmod -R g+rwX /app/data
VOLUME ["/app/data"]
USER 10001

EXPOSE 8080

# PYTHONDONTWRITEBYTECODE: the root filesystem is read-only at run time (the bytecode is compiled above).
# PENGUWATCH_HOST: listen on every interface of the container; the published port decides who can reach it.
ENV PENGUWATCH_DATA=/app/data PENGUWATCH_CONFIG=/app/config.yaml PENGUWATCH_HOST=0.0.0.0

# SIGTERM lets the app flush pending SIMKL changes before exiting
STOPSIGNAL SIGTERM
CMD ["penguwatch", "serve"]
