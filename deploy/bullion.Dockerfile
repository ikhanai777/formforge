# The bullion signal engine: API, dashboard and the ingestion cycle.
#
# Build:
#   docker build -f deploy/bullion.Dockerfile -t bullion:0.1.0 .
#
# Run the API and dashboard:
#   docker run --rm -p 8000:8000 \
#     -e TWELVEDATA_API_KEY=... -e GOLDAPI_KEY=... -e FINNHUB_KEY=... \
#     -e ANTHROPIC_API_KEY=... -e BULLION_ALLOW_SYNTHETIC=0 \
#     -v bullion-data:/data bullion:0.1.0
#
# Run one ingestion cycle (what a scheduler calls):
#   docker run --rm -v bullion-data:/data --env-file .env bullion:0.1.0 \
#     bullion signal --persist
#
# The engine's core needs nothing but the standard library, so this image carries
# only the web server and the optional Anthropic client. No numeric wheels, no C
# build step -- it builds in seconds and the attack surface is a dozen packages.

FROM python:3.11-slim

# A non-root user, and a data directory it owns. The store is the track record;
# losing it to a container restart would re-base the published win rate, so it
# must live on a mounted volume rather than in the image.
RUN useradd --create-home --uid 10001 bullion \
    && mkdir -p /data \
    && chown bullion:bullion /data

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BULLION_STORE=/data/bullion.db \
    # Production posture: a missing price key should page someone rather than
    # quietly serve generated prices. Override to 1 only for a demo.
    BULLION_ALLOW_SYNTHETIC=0 \
    PORT=8000

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
RUN pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "pydantic>=2.6" \
        "anthropic>=0.40"

COPY bullion/ /app/bullion/
COPY pyproject.toml README.md /app/

# --no-deps: the dependency set above is deliberate and pinned by this file.
# Installing formforge's geometry stack into a signal engine would add a CAD
# kernel to an image that never touches one.
RUN pip install --no-deps -e /app

USER bullion
EXPOSE 8000

# The API binds 0.0.0.0 in a container; the CLI default of 127.0.0.1 is for a laptop.
ENV BULLION_HOST=0.0.0.0

# A container that answers /health is not necessarily one with fresh data, so the
# check is liveness only. Data freshness is reported per-signal, in the payload.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=4).status==200 else 1)"

CMD ["bullion-api"]
