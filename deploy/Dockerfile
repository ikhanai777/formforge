# The FormForge application image: CLI, orchestrator, and HTTP gateway.
#
# Distinct from deploy/geometry.Dockerfile, which is the sandbox image and
# deliberately contains only the CAD kernel. This one has the parts that decide
# what to build; that one has the parts that build it.
#
#   docker build -f deploy/Dockerfile -t formforge/app:0.1.0 .
#
# Or, with a local model alongside it:
#   docker compose up -d

FROM python:3.11-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY formforge ./formforge

# Wheels are resolved once here and copied into the runtime stage, so the
# compiler never ships.
RUN pip install --no-cache-dir --prefix=/install ".[all]"


FROM python:3.11-slim

# libgl and friends are OCCT's; fonts are not optional. Three templates call
# Text() -- the monogram, the name tag and the medallion -- and on an image
# with no fonts installed they fail inside the kernel with an error that does
# not mention fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libxrender1 libxext6 \
        fonts-liberation fontconfig \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

COPY --from=build /install /usr/local
COPY formforge /opt/formforge/formforge
COPY pyproject.toml README.md /opt/formforge/
COPY deploy /opt/formforge/deploy

# Everything the process writes goes here, so the rest of the filesystem can be
# mounted read-only.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin forge \
    && mkdir -p /data /out \
    && chown -R forge:forge /data /out /opt/formforge

WORKDIR /opt/formforge
USER forge:forge

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    FORMFORGE_DB=/data/formforge.db \
    FORMFORGE_STORE=/data/models \
    PYTHONPATH=/opt/formforge

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["python", "-m", "uvicorn", "formforge.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]
