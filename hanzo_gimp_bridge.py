#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Hanzo AI
#
# hanzo_gimp_bridge.py -- clean-room BSD-3 GIMP 3.0 bridge plug-in.
#
# This is a GIMP 3.0 Python plug-in (GObject-Introspection style) that runs a
# small line-oriented JSON TCP server inside GIMP. External clients (the Hanzo
# MCP "gimp" tool, hanzo-tools-gimp, or anything that can open a socket) drive
# GIMP entirely through its documented Procedural Database (PDB) and the
# Gimp.* introspected API. No GPL lineage: every call below is derived from
# GIMP's own public API documentation.
#
# Protocol (see PROTOCOL.md):
#   request  : {"id": <any>, "method": <str>, "params": {<...>}}\n
#   response : {"id": <any>, "result": <any>}\n
#          or  {"id": <any>, "error": {"message": <str>, "type": <str>}}\n
# One JSON object per line, UTF-8, newline-terminated.

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import traceback
from typing import Any

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio, GLib, GObject  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults / configuration
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
BRIDGE_VERSION = "1.0.0"

# Procedure registered inside GIMP to start the bridge from the Filters menu.
PROC_START = "hanzo-gimp-bridge-start"


# ---------------------------------------------------------------------------
# GIMP value <-> JSON marshalling
# ---------------------------------------------------------------------------

def _gimp_object_to_id(obj: Any) -> Any:
    """Reduce a GIMP object (image/drawable/layer/...) to its integer id.

    GIMP 3.0 PDB procedures accept/return live objects, but a JSON wire
    protocol can only carry primitives, so we exchange integer ids and
    rehydrate them on demand via ``Gimp.Image.get_by_id`` and friends.
    """
    if obj is None:
        return None
    for getter in ("get_id",):
        fn = getattr(obj, getter, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                pass
    return obj


def _json_default(obj: Any) -> Any:
    """Best-effort JSON fallback for non-primitive GIMP/GLib values."""
    reduced = _gimp_object_to_id(obj)
    if reduced is not obj:
        return reduced
    if isinstance(obj, (bytes, bytearray)):
        return list(obj)
    return str(obj)


def _image_by_id(image_id: int) -> Gimp.Image:
    img = Gimp.Image.get_by_id(int(image_id))
    if img is None:
        raise ValueError(f"no image with id {image_id}")
    return img


# ---------------------------------------------------------------------------
# Generic PDB invocation
# ---------------------------------------------------------------------------

def _coerce_arg(spec: GObject.ParamSpec, value: Any) -> Any:
    """Coerce a JSON value to the type a PDB argument expects.

    Integer ids are rehydrated into the GIMP object the PDB wants
    (Image / Drawable / Layer / Item), so callers can pass plain ints.
    """
    if value is None:
        return None

    value_type = spec.value_type

    # Object arguments: accept an int id, fetch the live object.
    if GObject.type_is_a(value_type, Gimp.Image.__gtype__):
        return Gimp.Image.get_by_id(int(value))
    if GObject.type_is_a(value_type, Gimp.Item.__gtype__):
        # Covers Drawable, Layer, Channel, Vectors/Path -- all Items.
        return Gimp.Item.get_by_id(int(value))
    if GObject.type_is_a(value_type, Gio.File.__gtype__):
        return Gio.File.new_for_path(str(value))

    return value


def _pdb_call(procedure: str, args: list[Any]) -> Any:
    """Invoke an arbitrary PDB procedure -- the core power of the bridge.

    Args:
        procedure: PDB procedure name, e.g. "gimp-image-flatten".
        args: Positional arguments in PDB order. Integer ids are accepted
              where the procedure expects a GIMP object.

    Returns:
        ``None``                      if the procedure returns only a status,
        a single value                if it returns exactly one result,
        a list of values              if it returns several.
        GIMP objects are reduced to their integer ids.
    """
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure(procedure)
    if proc is None:
        raise ValueError(f"unknown PDB procedure: {procedure}")

    arg_specs = proc.get_arguments()
    config = proc.create_config()

    # The first PDB arg of most procedures is a run-mode; default it to
    # NONINTERACTIVE when the caller did not supply it.
    supplied = list(args)
    if arg_specs and arg_specs[0].value_type == Gimp.RunMode.__gtype__:
        if len(supplied) < len(arg_specs):
            supplied = [int(Gimp.RunMode.NONINTERACTIVE)] + supplied

    for spec, value in zip(arg_specs, supplied):
        config.set_property(spec.get_name(), _coerce_arg(spec, value))

    result = proc.run(config)

    status = result.index(0)
    if status != Gimp.PDBStatusType.SUCCESS:
        msg = ""
        try:
            msg = result.index(1)
        except Exception:
            pass
        raise RuntimeError(f"PDB {procedure} failed: {status.value_nick}: {msg}")

    # Collect return values after the status code.
    out: list[Any] = []
    for i in range(1, result.length()):
        out.append(_gimp_object_to_id(result.index(i)))

    if not out:
        return None
    if len(out) == 1:
        return out[0]
    return out


# ---------------------------------------------------------------------------
# High-level convenience methods
# ---------------------------------------------------------------------------

def _open_image(path: str) -> dict[str, Any]:
    gfile = Gio.File.new_for_path(path)
    image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, gfile)
    return {
        "image_id": _gimp_object_to_id(image),
        "width": image.get_width(),
        "height": image.get_height(),
    }


def _export(image_id: int, path: str) -> dict[str, Any]:
    image = _image_by_id(image_id)
    gfile = Gio.File.new_for_path(path)
    # Gimp.file_save dispatches to the right exporter by file extension and
    # works for the common formats (png/jpg/tiff/webp/...). It exports a
    # flattened copy for non-native formats; the .xcf path is GIMP-native.
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, gfile, None)
    return {"image_id": int(image_id), "path": path, "saved": True}


def _new_image(width: int, height: int) -> dict[str, Any]:
    image = Gimp.Image.new(int(width), int(height), Gimp.ImageBaseType.RGB)
    return {
        "image_id": _gimp_object_to_id(image),
        "width": image.get_width(),
        "height": image.get_height(),
    }


def _image_info(image_id: int) -> dict[str, Any]:
    image = _image_by_id(image_id)
    layers = [_gimp_object_to_id(l) for l in image.get_layers()]
    gfile = image.get_file()
    return {
        "image_id": int(image_id),
        "width": image.get_width(),
        "height": image.get_height(),
        "base_type": image.get_base_type().value_nick,
        "precision": image.get_precision().value_nick,
        "layers": layers,
        "n_layers": len(layers),
        "file": gfile.get_path() if gfile is not None else None,
        "dirty": bool(image.is_dirty()),
    }


def _list_procedures(prefix: str | None = None) -> dict[str, Any]:
    pdb = Gimp.get_pdb()
    names = list(pdb.query_procedures("", "", "", "", "", "", ""))
    if prefix:
        names = [n for n in names if n.startswith(prefix)]
    names.sort()
    return {"count": len(names), "procedures": names}


def _flatten(image_id: int) -> dict[str, Any]:
    image = _image_by_id(image_id)
    layer = image.flatten()
    return {"image_id": int(image_id), "layer_id": _gimp_object_to_id(layer)}


def _version() -> dict[str, Any]:
    return {
        "bridge": BRIDGE_VERSION,
        "gimp": Gimp.version(),
        "protocol": "hanzo-gimp/1",
    }


# ---------------------------------------------------------------------------
# Method dispatch
# ---------------------------------------------------------------------------

def dispatch(method: str, params: dict[str, Any]) -> Any:
    """Route a single JSON-protocol request to its handler."""
    params = params or {}

    if method == "version":
        return _version()
    if method == "pdb_call":
        return _pdb_call(params["procedure"], params.get("args", []))
    if method == "open_image":
        return _open_image(params["path"])
    if method == "export":
        return _export(params["image_id"], params["path"])
    if method == "new_image":
        return _new_image(params["width"], params["height"])
    if method == "image_info":
        return _image_info(params["image_id"])
    if method == "list_procedures":
        return _list_procedures(params.get("prefix"))
    if method == "flatten":
        return _flatten(params["image_id"])

    raise ValueError(f"unknown method: {method}")


# ---------------------------------------------------------------------------
# TCP JSON line server
# ---------------------------------------------------------------------------

class BridgeServer:
    """Line-oriented JSON TCP server that runs GIMP requests on the main loop.

    GIMP/GEGL are not thread-safe, so every request is marshalled onto GIMP's
    GLib main loop via ``GLib.idle_add`` and the socket thread blocks on the
    result. This keeps all PDB work on the thread GIMP expects.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        sys.stderr.write(
            f"[hanzo-gimp-bridge] listening on {self.host}:{self.port}\n"
        )
        sys.stderr.flush()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            while self._running:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    reply = self._process_line(line)
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return

    def _process_line(self, line: bytes) -> bytes:
        req_id: Any = None
        try:
            req = json.loads(line.decode("utf-8"))
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            if not method:
                raise ValueError("missing method")
            result = self._run_on_main(method, params)
            payload = {"id": req_id, "result": result}
        except Exception as exc:  # noqa: BLE001 -- report any failure to client
            payload = {
                "id": req_id,
                "error": {
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            }
        return (json.dumps(payload, default=_json_default) + "\n").encode("utf-8")

    def _run_on_main(self, method: str, params: dict[str, Any]) -> Any:
        """Run ``dispatch`` on GIMP's main loop and block for the result."""
        done = threading.Event()
        box: dict[str, Any] = {}

        def _invoke() -> bool:
            try:
                box["result"] = dispatch(method, params)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
                box["traceback"] = traceback.format_exc()
            finally:
                done.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_invoke)
        done.wait()
        if "error" in box:
            sys.stderr.write(box.get("traceback", ""))
            sys.stderr.flush()
            raise box["error"]
        return box.get("result")

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


# Keep a module-level reference so the server outlives do_run.
_SERVER: BridgeServer | None = None


def _resolve_port() -> int:
    env = os.environ.get("HANZO_GIMP_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_PORT


def _resolve_host() -> str:
    return os.environ.get("HANZO_GIMP_HOST", DEFAULT_HOST)


def start_bridge() -> BridgeServer:
    """Start (once) and return the module-level bridge server."""
    global _SERVER
    if _SERVER is None:
        _SERVER = BridgeServer(host=_resolve_host(), port=_resolve_port())
        _SERVER.start()
    return _SERVER


# ---------------------------------------------------------------------------
# GIMP 3.0 plug-in registration
# ---------------------------------------------------------------------------

class HanzoGimpBridge(Gimp.PlugIn):
    """GIMP 3.0 plug-in that exposes GIMP over a JSON TCP socket."""

    # -- Gimp.PlugIn vfuncs --------------------------------------------------

    def do_query_procedures(self) -> list[str]:
        return [PROC_START]

    def do_create_procedure(self, name: str) -> Gimp.Procedure:
        if name != PROC_START:
            return None

        procedure = Gimp.Procedure.new(
            self,
            name,
            Gimp.PDBProcType.PLUGIN,
            self.run_start,
            None,
        )
        procedure.set_menu_label("Start Hanzo Bridge")
        procedure.add_menu_path("<Image>/Filters/Hanzo")
        procedure.set_documentation(
            "Start the Hanzo GIMP JSON socket bridge",
            "Runs a local JSON-over-TCP server that drives GIMP through its "
            "PDB API. Clean-room BSD-3, no GPL lineage.",
            name,
        )
        procedure.set_attribution("Hanzo AI", "Hanzo AI", "2026")
        return procedure

    # -- Procedure run callback ---------------------------------------------

    def run_start(self, procedure, run_mode, image, drawables, config, run_data):
        try:
            server = start_bridge()
            Gimp.message(
                f"Hanzo GIMP bridge listening on "
                f"{server.host}:{server.port}"
            )
            return procedure.new_return_values(
                Gimp.PDBStatusType.SUCCESS, GLib.Error()
            )
        except Exception as exc:  # noqa: BLE001
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR,
                GLib.Error(str(exc)),
            )


Gimp.main(HanzoGimpBridge.__gtype__, sys.argv)
