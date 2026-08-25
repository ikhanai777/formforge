# The geometry sandbox image.
#
# This container executes model-authored Python. It should therefore contain the
# CAD kernel and nothing else -- not the API layer, not the model client, not the
# template registry. Anything present here is reachable from generated code, and
# the smallest thing that can be reached is the safest.
#
# Build:
#   docker build -f deploy/geometry.Dockerfile -t formforge/geometry:0.1.0 .
#
# Run (as the executor does):
#   docker run --rm -i --runtime=runsc --network=none --read-only \
#     --cap-drop=ALL --security-opt=no-new-privileges --user=65534:65534 \
#     --pids-limit=64 --memory=2048m --memory-swap=2048m \
#     --tmpfs=/work:rw,size=256m,mode=1777 --env-file=/dev/null \
#     --security-opt=seccomp=deploy/seccomp.json \
#     formforge/geometry:0.1.0 python -I /opt/formforge/runner.py

FROM python:3.11-slim AS build

# OCCT needs a handful of shared libraries; the wheel bundles the rest.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Only the geometry stack. Deliberately no anthropic, no fastapi, no yaml --
# the sandbox never parses a template or talks to a model.
RUN pip install --no-cache-dir --prefix=/install \
        "build123d>=0.11" \
        "trimesh>=4.0" \
        "numpy>=1.26"

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/* \
    && find / -xdev -name '*.pyc' -delete

COPY --from=build /install /usr/local

# The runner is copied on its own, by design: it imports nothing from the rest
# of FormForge, so the package never has to be present in this image.
COPY formforge/sandbox/runner.py /opt/formforge/runner.py

# Writable only where the job needs it. The executor mounts /work as a tmpfs.
RUN mkdir -p /work && chown 65534:65534 /work

USER 65534:65534
WORKDIR /work

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    MPLBACKEND=Agg

# -I isolates the interpreter: no PYTHONPATH, no user site directory, no
# environment influence on module resolution.
ENTRYPOINT ["python", "-I", "/opt/formforge/runner.py"]
