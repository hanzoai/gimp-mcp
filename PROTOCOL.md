# Hanzo GIMP Bridge Protocol

A line-oriented JSON protocol over a plain TCP socket. The bridge plug-in
(`hanzo_gimp_bridge.py`) runs inside GIMP 3.0 and listens, by default, on
`127.0.0.1:9876` (override with the `HANZO_GIMP_PORT` / `HANZO_GIMP_HOST`
environment variables before starting GIMP).

This is a **clean-room BSD-3** design. Every operation maps onto GIMP's own
documented Procedural Database (PDB) and the `Gimp.*` GObject-Introspection
API. There is no GPL lineage.

## Framing

* One JSON object per line, UTF-8, terminated by a single `\n`.
* The client sends one **request** object per line.
* The server replies with exactly one **response** object per request,
  newline-terminated.
* Multiple requests may be pipelined on one connection.

### Request

```json
{"id": <any>, "method": "<string>", "params": { ... }}
```

* `id` — any JSON value; echoed back so clients can correlate replies.
* `method` — one of the methods below.
* `params` — method-specific object (may be omitted / empty).

### Response

Success:

```json
{"id": <any>, "result": <any>}
```

Error:

```json
{"id": <any>, "error": {"message": "<string>", "type": "<ExceptionClass>"}}
```

## Object ids

GIMP 3.0 PDB procedures operate on live objects (images, layers, drawables,
paths). Because JSON can only carry primitives, the bridge exchanges **integer
ids**. Where a PDB procedure expects an `Image`, `Item`, `Drawable`, `Layer`,
`Channel`, or `Path`, pass its integer id and the bridge rehydrates the live
object. Returned objects are likewise reduced to integer ids.

## Methods

| Method            | Params                                   | Result |
|-------------------|------------------------------------------|--------|
| `version`         | —                                        | `{bridge, gimp, protocol}` |
| `pdb_call`        | `procedure: str`, `args: list`           | the procedure's return value(s) (ids for objects), or `null` |
| `open_image`      | `path: str`                              | `{image_id, width, height}` |
| `export`          | `image_id: int`, `path: str`            | `{image_id, path, saved}` |
| `new_image`       | `width: int`, `height: int`             | `{image_id, width, height}` |
| `image_info`      | `image_id: int`                          | `{image_id, width, height, base_type, precision, layers, n_layers, file, dirty}` |
| `list_procedures` | `prefix?: str`                           | `{count, procedures: [str, ...]}` |
| `flatten`         | `image_id: int`                          | `{image_id, layer_id}` |

### `pdb_call` — the core power

`pdb_call` is a generic invocation of any procedure in GIMP's PDB. Anything
GIMP can do is reachable through it. The bridge:

1. Looks up the procedure by name.
2. If the first PDB argument is a run-mode and you did not supply one,
   prepends `NONINTERACTIVE`.
3. Coerces integer ids in `args` to the live `Image` / `Item` / `Gio.File`
   objects the procedure expects.
4. Runs it and returns the values after the PDB status code (object results
   reduced to ids). A non-`SUCCESS` status becomes an `error` response.

Use `list_procedures` to discover names (e.g. prefix `"gimp-image-"`).

## Examples

Assuming a raw TCP connection to `127.0.0.1:9876`:

**Version**

```
--> {"id": 1, "method": "version"}
<-- {"id": 1, "result": {"bridge": "1.0.0", "gimp": "3.0.0", "protocol": "hanzo-gimp/1"}}
```

**Open an image**

```
--> {"id": 2, "method": "open_image", "params": {"path": "/tmp/in.png"}}
<-- {"id": 2, "result": {"image_id": 1, "width": 1920, "height": 1080}}
```

**Generic PDB call — Gaussian blur on the active drawable**

```
--> {"id": 3, "method": "pdb_call",
     "params": {"procedure": "plug-in-gauss", "args": [1, <drawable_id>, 15, 15, 0]}}
<-- {"id": 3, "result": null}
```

**Flatten and export**

```
--> {"id": 4, "method": "flatten", "params": {"image_id": 1}}
<-- {"id": 4, "result": {"image_id": 1, "layer_id": 7}}
--> {"id": 5, "method": "export", "params": {"image_id": 1, "path": "/tmp/out.png"}}
<-- {"id": 5, "result": {"image_id": 1, "path": "/tmp/out.png", "saved": true}}
```

**Discover procedures**

```
--> {"id": 6, "method": "list_procedures", "params": {"prefix": "gimp-image-"}}
<-- {"id": 6, "result": {"count": 42, "procedures": ["gimp-image-add-layer", ...]}}
```

**Error shape**

```
--> {"id": 7, "method": "pdb_call", "params": {"procedure": "no-such-proc", "args": []}}
<-- {"id": 7, "error": {"message": "unknown PDB procedure: no-such-proc", "type": "ValueError"}}
```

## Concurrency

GIMP/GEGL are single-threaded. The bridge accepts connections on worker
threads but marshals every request onto GIMP's GLib main loop, so operations
execute serially in the order GIMP receives them.
