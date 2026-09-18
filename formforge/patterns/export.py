"""Writing a tile out.

STL is written by trimesh. 3MF is written here, by hand, for one reason: the
unit declaration. Every "my print came out 25.4 times too big" report in this
product category traces back to STL having no units, and 3MF's answer is a
single attribute -- `unit="millimeter"` on the `<model>` element -- that the
slicer reads and acts on.

A 3MF file is a zip with three entries, and producing it directly costs about
sixty lines. The alternative is an XML library that is not otherwise a
dependency of this project, pulled in so that a third-party exporter can write
the same three entries. That trade is not worth making for a format this
small, and doing it here means the unit attribute is set by us rather than
hoped for.

Only the core specification is emitted: one object, one mesh, one build item.
No materials, no colours, no print settings -- a relief tile has no use for
them, and every slicer in circulation reads this subset.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import trimesh

__all__ = ["write_3mf", "write_stl"]

# Media types, named by the 3MF core specification.
_RELS_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
_MODEL_TYPE = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"

_CONTENT_TYPES = f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="{_RELS_TYPE}"/>
  <Default Extension="model" ContentType="{_MODEL_TYPE}"/>
</Types>
"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0"
    Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

_MODEL_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def write_stl(mesh: trimesh.Trimesh, path: str | Path) -> str:
    """Write a binary STL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(path))
    return str(path)


def write_3mf(mesh: trimesh.Trimesh, path: str | Path, *, name: str = "tile") -> str:
    """Write a minimal, unit-declaring 3MF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if len(faces) == 0:
        raise ValueError("refusing to write a 3MF with no triangles")

    # 3MF requires every coordinate to be finite and the winding to be
    # counter-clockwise seen from outside, which is what the mesh builder and
    # the boolean checks have already established.
    if not np.isfinite(vertices).all():
        raise ValueError("the mesh contains non-finite coordinates")

    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{_MODEL_NS}">\n',
        '  <metadata name="Application">FormForge</metadata>\n',
        f'  <metadata name="Title">{escape(name)}</metadata>\n',
        "  <resources>\n",
        '    <object id="1" type="model">\n',
        "      <mesh>\n",
        "        <vertices>\n",
    ]
    chunks.extend(
        f'          <vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>\n' for x, y, z in vertices
    )
    chunks.append("        </vertices>\n        <triangles>\n")
    chunks.extend(f'          <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in faces)
    chunks += [
        "        </triangles>\n",
        "      </mesh>\n",
        "    </object>\n",
        "  </resources>\n",
        "  <build>\n",
        '    <item objectid="1"/>\n',
        "  </build>\n",
        "</model>\n",
    ]

    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("3D/3dmodel.model", "".join(chunks))
    return str(path)
