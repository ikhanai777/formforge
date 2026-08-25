"""stdio entrypoint for the FormForge MCP server.

    npx-style local use:  python -m formforge.mcp

Connects the transport-agnostic tool layer in `server.py` to the official MCP
SDK. Everything meaningful lives in `FormForgeTools`; this file is wiring, and
deliberately so -- the hosted remote server (Streamable HTTP + OAuth 2.1) and
the REST API bind the same object, so the three transports cannot drift into
behaving differently for the same call.

Resources and prompts are registered alongside the tools (spec section 9.3):
`formforge://templates/{id}` for a template definition, `formforge://profiles/{id}`
for a printer profile, and a `design_review` prompt for walking a DFM report
conversationally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ..dfm import PROFILES, rules_block
from .server import TOOL_DEFINITIONS, FormForgeTools, ToolError, result_content

log = logging.getLogger("formforge.mcp")

SERVER_NAME = "formforge"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
FormForge turns descriptions into print-ready 3D models. It does not generate \
meshes -- it drives a parametric CAD kernel, so dimensions are exact and every \
result is watertight by construction.

Suggested flow:

1. `list_templates` first. A matching template is faster, cheaper and \
verified, and it cannot produce broken geometry. Templates report a \
`print_test_status`; treat `untested` as "validated but never physically \
printed" and say so if the user is about to print it.
2. `get_template_schema` to see the valid ranges, the DFM note on each \
parameter, and the cross-parameter requirements. The notes explain why each \
default is what it is.
3. `generate_from_template` with your values.
4. Look at the returned preview images before telling the user it is done. The \
section view is the only way to see inside a hollow part.
5. `export_model` for the files. Recommend the 3MF: it records its units, and \
STL does not.

Use `generate_from_prompt` when the user describes something rather than naming \
a template, and `generate_from_code` only when nothing in the registry fits."""


def build_server(tools: FormForgeTools) -> Server:
    server = Server(SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=definition["name"],
                description=definition["description"],
                inputSchema=definition["input_schema"],
            )
            for definition in TOOL_DEFINITIONS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        try:
            # The tool layer is synchronous and CPU-bound -- it runs a CAD
            # kernel and a rasteriser. Running it in a thread keeps the event
            # loop responsive so the transport can still answer pings during a
            # 20-second generation.
            payload = await asyncio.to_thread(tools.call, name, arguments)
        except ToolError as exc:
            return [types.TextContent(type="text", text=f"Error: {exc}")]
        except Exception as exc:  # noqa: BLE001 - surface, never crash the server
            log.exception("tool %s failed", name)
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: {name} failed unexpectedly: {type(exc).__name__}: {exc}",
                )
            ]
        return _to_content(payload)

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        resources = [
            types.Resource(
                uri=f"formforge://templates/{template.id}",
                name=template.display_name,
                description=template.description.strip()[:200],
                mimeType="application/json",
            )
            for template in tools.registry.all()
        ]
        resources += [
            types.Resource(
                uri=f"formforge://profiles/{profile.id}",
                name=profile.display_name,
                description=(
                    f"{profile.nozzle_mm} mm nozzle, {profile.layer_mm} mm layer, "
                    f"{profile.build_volume_mm[0]:.0f}x{profile.build_volume_mm[1]:.0f}x"
                    f"{profile.build_volume_mm[2]:.0f} mm build volume"
                ),
                mimeType="application/json",
            )
            for profile in PROFILES.values()
        ]
        resources.append(
            types.Resource(
                uri="formforge://rules/dfm",
                name="Design-for-manufacturing rules",
                description=(
                    "The wall thicknesses, clearances, overhang limits and text "
                    "minimums every generated model is checked against."
                ),
                mimeType="text/plain",
            )
        )
        return resources

    @server.read_resource()
    async def read_resource(uri) -> str:
        text = str(uri)
        if text == "formforge://rules/dfm":
            return rules_block()
        if text.startswith("formforge://templates/"):
            template_id = text.rsplit("/", 1)[-1]
            try:
                return json.dumps(tools.registry.get(template_id).detail(), indent=2)
            except KeyError as exc:
                raise ValueError(str(exc)) from None
        if text.startswith("formforge://profiles/"):
            profile_id = text.rsplit("/", 1)[-1]
            profile = PROFILES.get(profile_id)
            if profile is None:
                raise ValueError(f"no printer profile {profile_id!r}")
            return json.dumps(
                {
                    "id": profile.id,
                    "display_name": profile.display_name,
                    "nozzle_mm": profile.nozzle_mm,
                    "layer_mm": profile.layer_mm,
                    "build_volume_mm": list(profile.build_volume_mm),
                    "material": profile.material,
                    "dfm_rules": rules_block(profile.id),
                },
                indent=2,
            )
        raise ValueError(f"unknown resource {text!r}")

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return [
            types.Prompt(
                name="design_review",
                description=(
                    "Walk through a model's manufacturability report "
                    "conversationally, explaining what each finding means for the "
                    "print and what to change."
                ),
                arguments=[
                    types.PromptArgument(
                        name="model_id",
                        description="The model to review.",
                        required=True,
                    )
                ],
            ),
            types.Prompt(
                name="remix",
                description=(
                    "Guide a modification of an existing model: what the "
                    "parameters do, which ones are safe to change, and what each "
                    "will cost in print time or strength."
                ),
                arguments=[
                    types.PromptArgument(
                        name="model_id", description="The model to modify.", required=True
                    ),
                    types.PromptArgument(
                        name="goal",
                        description="What the user wants to change.",
                        required=False,
                    ),
                ],
            ),
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
        arguments = arguments or {}
        model_id = arguments.get("model_id", "")
        if name == "design_review":
            text = (
                f"Run `check_printability` on model {model_id}, then walk me "
                "through the report. For each failure and warning, tell me what "
                "it means for the actual print, how bad it is, and what parameter "
                "to change. Skip the checks that passed. Also render the section "
                "view and tell me whether the walls look right."
            )
        elif name == "remix":
            goal = arguments.get("goal", "")
            text = (
                f"Look at model {model_id}: get its template schema and current "
                "parameters. Explain what each parameter does and which ones are "
                "safe to change."
                + (f" I want to: {goal}." if goal else "")
                + " Then propose specific values and use `modify_model` to build "
                "it, telling me what the change costs in print time or strength."
            )
        else:
            raise ValueError(f"unknown prompt {name!r}")

        return types.GetPromptResult(
            description=f"FormForge {name}",
            messages=[
                types.PromptMessage(
                    role="user", content=types.TextContent(type="text", text=text)
                )
            ],
        )

    return server


def _to_content(payload: dict) -> list[types.ContentBlock]:
    """Tool result to MCP content blocks, with previews inline.

    Returning the image is the point (spec section 9.4): Claude can see the
    result and correct itself in the same turn instead of describing a file it
    has not looked at.
    """
    blocks: list[types.ContentBlock] = []
    for block in result_content(payload):
        if block["type"] == "text":
            blocks.append(types.TextContent(type="text", text=block["text"]))
        else:
            blocks.append(
                types.ImageContent(
                    type="image", data=block["data"], mimeType=block["mimeType"]
                )
            )
    return blocks


async def _serve() -> None:
    store = os.environ.get("FORMFORGE_STORE")
    tools = FormForgeTools(store_dir=Path(store) if store else None)
    server = build_server(tools)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main() -> int:
    # stderr, never stdout: stdout is the JSON-RPC transport and a stray log
    # line there corrupts the protocol stream.
    logging.basicConfig(
        level=os.environ.get("FORMFORGE_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
