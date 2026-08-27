"""Reference reconstruction backend, to run on the machine with the GPU.

FormForge does not ship weights and does not run a reconstruction model. This
is the small piece that sits between `formforge reconstruct` and whatever model
you installed, so the model stays your choice and your hardware.

    pip install "fastapi" "uvicorn[standard]" python-multipart trimesh
    python deploy/reconstruct_server.py                 # your model
    FORMFORGE_RECON_DEMO=1 python deploy/reconstruct_server.py   # placeholder

The contract is one endpoint:

    POST /reconstruct
      multipart/form-data
        image   one or more image files, one per view
        format  glb | obj | ply | stl
      200 -> the mesh bytes, with a mesh Content-Type

Demo mode returns a deliberately broken placeholder -- floaters, a hole, unit
scale -- so you can watch the clean-up ladder work before committing to a
download. It is obviously not a reconstruction of anything, which is the point:
nothing that comes out of demo mode should ever be mistaken for a result.

Plugging in a real model means filling in `run_model`. The multi-view models
worth looking at take a list of images directly:

    TRELLIS          microsoft/TRELLIS          image-to-3D, strong meshes
    Hunyuan3D-2mv    Tencent                    built for multi-view input
    InstantMesh      TencentARC                 sparse views, fast
    TripoSR          VAST-AI / Stability        single image, very fast

A word on the four product views: they are not a photogrammetry input. Classic
structure-from-motion needs many overlapping, texture-rich frames with
consistent lighting, and four studio shots ninety degrees apart on a white
background give it nothing to match. A learned multi-view model is the route
that actually works on images like those.
"""

import os
import tempfile
from pathlib import Path

try:
    # At module scope, not inside build_app: FastAPI resolves the endpoint
    # annotations against module globals, and a name only visible inside the
    # factory comes back to it as an unresolvable forward reference.
    from fastapi import FastAPI, File, Form, UploadFile
    from fastapi.responses import Response
except ModuleNotFoundError:  # pragma: no cover - a machine without the extras
    FastAPI = None


CONTENT_TYPES = {
    "glb": "model/gltf-binary",
    "obj": "model/obj",
    "ply": "model/ply",
    "stl": "model/stl",
}


def run_model(image_paths: list[Path], fmt: str) -> bytes:
    """Reconstruct a mesh from one or more views and return it encoded as `fmt`.

    Replace the body with a call into your model. Most of them expose either a
    Python entry point or a pipeline object; the shape of this function is
    chosen so that either fits:

        from trellis.pipelines import TrellisImageTo3DPipeline
        pipe = TrellisImageTo3DPipeline.from_pretrained("microsoft/TRELLIS-image-large")
        out = pipe.run_multi_image([Image.open(p) for p in image_paths])
        mesh = out["mesh"][0]
        ... export to `fmt` and return the bytes ...

    Keep it synchronous. `formforge reconstruct` waits for the mesh rather than
    polling, and a reconstruction that takes four minutes is fine -- the
    client's --timeout defaults to fifteen.
    """
    if os.environ.get("FORMFORGE_RECON_DEMO") == "1":
        return _demo_mesh(fmt)
    raise NotImplementedError(
        "No reconstruction model is wired in. Fill in run_model() in this file, "
        "or set FORMFORGE_RECON_DEMO=1 to exercise the pipeline with a "
        "placeholder."
    )


def _demo_mesh(fmt: str) -> bytes:
    """A placeholder that is wrong in the ways a real one usually is.

    Unit-scaled, carrying a stray shard, and with a face removed so it is not
    watertight. It exists to prove the clean-up ladder runs, not to look like
    anything.
    """
    import trimesh

    body = trimesh.creation.box(extents=(1.0, 1.0, 1.6)).subdivide().subdivide()
    body.faces = body.faces[:-2]          # tear a hole in it
    shard = trimesh.creation.box(extents=(0.08, 0.08, 0.08))
    shard.apply_translation([1.4, 0.9, 0.4])   # a floater, well clear of the body
    combined = trimesh.util.concatenate([body, shard])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"demo.{fmt}"
        combined.export(str(out))
        return out.read_bytes()


def build_app():
    app = FastAPI(title="FormForge reconstruction backend")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "demo": os.environ.get("FORMFORGE_RECON_DEMO") == "1",
        }

    @app.post("/reconstruct")
    async def reconstruct(
        # A call in a default is how FastAPI declares form fields; the usual
        # objection to it does not apply here.
        image: list[UploadFile] = File(...),  # noqa: B008
        format: str = Form("glb"),
    ):
        fmt = format.lower().lstrip(".")
        if fmt not in CONTENT_TYPES:
            return Response(
                content=f"unsupported format {fmt!r}", status_code=400, media_type="text/plain"
            )
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index, upload in enumerate(image):
                suffix = Path(upload.filename or f"view{index}.png").suffix or ".png"
                path = Path(tmp) / f"view{index}{suffix}"
                path.write_bytes(await upload.read())
                paths.append(path)
            payload = run_model(paths, fmt)
        return Response(content=payload, media_type=CONTENT_TYPES[fmt])

    return app


app = build_app() if FastAPI is not None else None


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=int(os.environ.get("PORT", 8300)))
