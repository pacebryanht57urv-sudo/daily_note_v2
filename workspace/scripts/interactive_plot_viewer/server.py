#!/usr/bin/env python
"""Local interactive viewer for comparing .npz and .mat traces.

The server only reads files under DAILY_NOTE_DATA_ROOT or an explicit
--data-root. It does not write figures or data into the repository.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import threading
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

try:
    from scipy.io import loadmat
except Exception:  # pragma: no cover - reported through /api/status
    loadmat = None

try:
    import h5py
except Exception:  # pragma: no cover - optional v7.3 MATLAB support
    h5py = None


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
SUPPORTED_SUFFIXES = {".npz", ".mat"}
PLOTLY_VERSION = "2.35.2"
PLOTLY_URL = f"https://cdn.plot.ly/plotly-{PLOTLY_VERSION}.min.js"


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def is_numeric_array(value: object) -> bool:
    try:
        array = np.asarray(value)
    except Exception:
        return False
    return np.issubdtype(array.dtype, np.number) or np.issubdtype(array.dtype, np.bool_)


def describe_array(name: str, value: object) -> dict[str, object] | None:
    if not is_numeric_array(value):
        return None
    array = np.asarray(value)
    if array.ndim == 0:
        return None
    squeezed = np.squeeze(array)
    return {
        "name": name,
        "shape": list(array.shape),
        "squeezed_shape": list(squeezed.shape),
        "dtype": str(array.dtype),
        "ndim": int(squeezed.ndim),
        "size": int(array.size),
        "complex": bool(np.iscomplexobj(array)),
        "usable": bool(1 <= squeezed.ndim <= 2),
    }


def read_npz(path: Path) -> dict[str, np.ndarray]:
    arrays: dict[str, np.ndarray] = {}
    with np.load(path, allow_pickle=False) as loaded:
        for name in loaded.files:
            arrays[name] = np.asarray(loaded[name])
    return arrays


def read_mat(path: Path) -> dict[str, np.ndarray]:
    if loadmat is not None:
        try:
            loaded = loadmat(path, squeeze_me=False, struct_as_record=False)
            return {
                name: np.asarray(value)
                for name, value in loaded.items()
                if not name.startswith("__") and is_numeric_array(value)
            }
        except NotImplementedError:
            pass
        except ValueError as exc:
            if "Unknown mat file type" not in str(exc):
                raise

    if h5py is None:
        raise RuntimeError(
            "This .mat file looks like MATLAB v7.3/HDF5. Install h5py to inspect it."
        )

    arrays: dict[str, np.ndarray] = {}
    with h5py.File(path, "r") as handle:
        def visitor(name: str, node: object) -> None:
            if hasattr(node, "shape") and hasattr(node, "dtype"):
                value = np.asarray(node)
                if is_numeric_array(value):
                    arrays[name] = value

        handle.visititems(visitor)
    return arrays


def read_arrays(path: Path) -> dict[str, np.ndarray]:
    suffix = path.suffix.lower()
    if suffix == ".npz":
        return read_npz(path)
    if suffix == ".mat":
        return read_mat(path)
    raise ValueError(f"Unsupported file suffix: {suffix}")


def safe_resolve(data_root: Path, rel_path: str) -> Path:
    rel = Path(unquote(rel_path))
    if rel.is_absolute():
        raise ValueError("Use a path relative to the data root.")
    resolved = (data_root / rel).resolve()
    data_root_resolved = data_root.resolve()
    if resolved != data_root_resolved and data_root_resolved not in resolved.parents:
        raise ValueError("Path escapes the configured data root.")
    return resolved


def select_component(array: np.ndarray, component: str) -> np.ndarray:
    if not np.iscomplexobj(array):
        return array.astype(float, copy=False)
    if component == "real":
        return np.real(array)
    if component == "imag":
        return np.imag(array)
    if component == "abs":
        return np.abs(array)
    if component == "phase":
        return np.angle(array)
    raise ValueError(f"Unknown complex component: {component}")


def vector_from_spec(arrays: dict[str, np.ndarray], spec: dict[str, object]) -> np.ndarray:
    name = str(spec.get("name", ""))
    if not name:
        raise ValueError("Missing variable name.")
    if name not in arrays:
        raise ValueError(f"Variable not found: {name}")

    component = str(spec.get("component") or "real")
    array = select_component(np.asarray(arrays[name]), component)
    array = np.squeeze(array)

    if array.ndim == 0:
        raise ValueError(f"{name} is scalar, not a trace.")
    if array.ndim == 1:
        return np.asarray(array, dtype=float)
    if array.ndim != 2:
        raise ValueError(f"{name} has {array.ndim} dimensions after squeeze; only 1D/2D is supported.")

    mode = str(spec.get("mode") or "row")
    index = int(spec.get("index") or 0)
    if mode == "row":
        if not 0 <= index < array.shape[0]:
            raise ValueError(f"Row index {index} is out of range for {name}.")
        return np.asarray(array[index, :], dtype=float)
    if mode == "col":
        if not 0 <= index < array.shape[1]:
            raise ValueError(f"Column index {index} is out of range for {name}.")
        return np.asarray(array[:, index], dtype=float)
    if mode == "mean_rows":
        return np.asarray(np.nanmean(array, axis=0), dtype=float)
    if mode == "mean_cols":
        return np.asarray(np.nanmean(array, axis=1), dtype=float)
    raise ValueError(f"Unknown 2D mode: {mode}")


def finite_pair(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    count = min(len(x), len(y))
    if count == 0:
        raise ValueError("Trace has no points.")
    x = x[:count]
    y = y[:count]
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]
    if len(x) == 0:
        raise ValueError("Trace has no finite x/y pairs.")
    return x, y


def downsample_pair(x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray, bool]:
    if max_points <= 0 or len(x) <= max_points:
        return x, y, False
    step = int(np.ceil(len(x) / max_points))
    return x[::step], y[::step], True


class PlotViewerHandler(BaseHTTPRequestHandler):
    data_root: Path
    max_points: int
    plotly_js_path: Path | None

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_payload(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_payload(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self.send_payload({"ok": False, "error": message}, status)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self.handle_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path != "/api/trace":
            self.send_error_payload("Unknown POST endpoint.", HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        try:
            self.handle_trace(payload)
        except Exception as exc:
            self.send_error_payload(str(exc))

    def handle_static(self, path: str) -> None:
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        if rel == "vendor/plotly.min.js":
            self.handle_plotly_vendor()
            return
        target = (STATIC_ROOT / rel).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_plotly_vendor(self) -> None:
        if self.plotly_js_path and self.plotly_js_path.exists():
            body = self.plotly_js_path.read_bytes()
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = (
            "window.Plotly = null;"
            "console.error('Plotly vendor file is missing. "
            "Run the server with --download-plotly or --plotly-js PATH.');"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/status":
                self.send_payload({
                    "ok": True,
                    "data_root": str(self.data_root),
                    "numpy": np.__version__,
                    "scipy_loadmat": loadmat is not None,
                    "h5py": h5py is not None,
                    "max_points": self.max_points,
                    "plotly_js": str(self.plotly_js_path) if self.plotly_js_path else "",
                })
                return
            if path == "/api/list":
                rel = query.get("path", [""])[0]
                self.handle_list(rel)
                return
            if path == "/api/inspect":
                rel = query.get("path", [""])[0]
                self.handle_inspect(rel)
                return
            self.send_error_payload("Unknown API endpoint.", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_payload(str(exc))

    def handle_list(self, rel: str) -> None:
        target = safe_resolve(self.data_root, rel)
        if not target.exists() or not target.is_dir():
            raise ValueError("Directory does not exist under the data root.")

        dirs = []
        files = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.name.startswith("."):
                continue
            child_rel = child.relative_to(self.data_root).as_posix()
            if child.is_dir():
                dirs.append({"name": child.name, "path": child_rel})
            elif child.suffix.lower() in SUPPORTED_SUFFIXES:
                files.append({
                    "name": child.name,
                    "path": child_rel,
                    "size": child.stat().st_size,
                    "suffix": child.suffix.lower(),
                })
        parent = ""
        if target.resolve() != self.data_root.resolve():
            parent = target.parent.relative_to(self.data_root).as_posix()
        self.send_payload({"ok": True, "path": rel, "parent": parent, "dirs": dirs, "files": files})

    def handle_inspect(self, rel: str) -> None:
        target = safe_resolve(self.data_root, rel)
        if not target.exists() or not target.is_file():
            raise ValueError("Data file does not exist under the data root.")
        arrays = read_arrays(target)
        variables = []
        for name, value in sorted(arrays.items()):
            description = describe_array(name, value)
            if description is not None:
                variables.append(description)
        self.send_payload({"ok": True, "path": rel, "variables": variables})

    def handle_trace(self, payload: dict[str, object]) -> None:
        rel = str(payload.get("path") or "")
        target = safe_resolve(self.data_root, rel)
        if not target.exists() or not target.is_file():
            raise ValueError("Data file does not exist under the data root.")

        arrays = read_arrays(target)
        y_spec = payload.get("y")
        if not isinstance(y_spec, dict):
            raise ValueError("Missing y variable spec.")
        y = vector_from_spec(arrays, y_spec)

        x_spec = payload.get("x")
        if isinstance(x_spec, dict) and x_spec.get("name"):
            x = vector_from_spec(arrays, x_spec)
        else:
            x = np.arange(len(y), dtype=float)

        original_points = int(min(len(x), len(y)))
        x, y = finite_pair(x, y)
        x, y, downsampled = downsample_pair(x, y, self.max_points)

        self.send_payload({
            "ok": True,
            "path": rel,
            "x": x.tolist(),
            "y": y.tolist(),
            "points": int(len(x)),
            "original_points": original_points,
            "downsampled": downsampled,
        })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive npz/mat comparison plot viewer.")
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DAILY_NOTE_DATA_ROOT"),
        help="External data root. Defaults to DAILY_NOTE_DATA_ROOT.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-points", type=int, default=50000)
    parser.add_argument("--open-browser", action="store_true", help="Open the viewer URL after the server starts.")
    parser.add_argument(
        "--plotly-js",
        default=os.environ.get("PLOTLY_JS_PATH"),
        help="Path to a local plotly.min.js file. Defaults to cached Plotly if present.",
    )
    parser.add_argument(
        "--download-plotly",
        action="store_true",
        help="Download Plotly once into the user cache if it is not already cached.",
    )
    return parser.parse_args()


def plotly_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "daily_note_v2" / "interactive_plot_viewer" / f"plotly-{PLOTLY_VERSION}.min.js"
    return Path.home() / ".cache" / "daily_note_v2" / "interactive_plot_viewer" / f"plotly-{PLOTLY_VERSION}.min.js"


def resolve_plotly_path(path_arg: str | None, download: bool) -> Path | None:
    if path_arg:
        path = Path(path_arg).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Plotly JS file does not exist: {path}")
        return path

    cached = plotly_cache_path()
    if cached.exists():
        return cached

    if download:
        cached.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading Plotly {PLOTLY_VERSION} to {cached}")
        with urllib.request.urlopen(PLOTLY_URL, timeout=60) as response:
            with cached.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        return cached

    return None


def main() -> int:
    args = parse_args()
    if not args.data_root:
        print(
            "ERROR: Set DAILY_NOTE_DATA_ROOT or pass --data-root. "
            "This viewer will not default to a repository data directory.",
            file=sys.stderr,
        )
        return 2

    data_root = Path(args.data_root).expanduser().resolve()
    if not data_root.exists() or not data_root.is_dir():
        print(f"ERROR: data root does not exist or is not a directory: {data_root}", file=sys.stderr)
        return 2

    try:
        plotly_js_path = resolve_plotly_path(args.plotly_js, args.download_plotly)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    handler = type(
        "ConfiguredPlotViewerHandler",
        (PlotViewerHandler,),
        {"data_root": data_root, "max_points": args.max_points, "plotly_js_path": plotly_js_path},
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Interactive plot viewer: {url}")
    print(f"Data root: {data_root}")
    if plotly_js_path:
        print(f"Plotly JS: {plotly_js_path}")
    else:
        print("Plotly JS: missing; use --download-plotly or --plotly-js PATH.")
    print("Press Ctrl+C to stop.")
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
