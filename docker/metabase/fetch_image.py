"""Fetch the official Metabase image via host HTTPS, then import with docker load.

Fallback when Docker Desktop image pulls fail with EOF. Run at project root:
    python docker/metabase/fetch_image.py
Uses only the Python standard library; verifies compressed and raw layer digests.
"""

import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "outputs" / "metabase_image_cache"
REPOSITORY = "metabase/metabase"
TAG = "v0.63.19"
BASE = f"https://registry-1.docker.io/v2/{REPOSITORY}/"
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_hex(value):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Unsupported image digest")
    return value.split(":", 1)[1]


def fetch_json(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=45) as response:
        return json.load(response)


def download_blob(descriptor, token):
    digest = digest_hex(descriptor["digest"])
    size = descriptor["size"]
    target = CACHE / (digest + ".blob")
    partial = CACHE / (digest + ".part")
    if target.exists() and target.stat().st_size == size and sha256(target) == digest:
        print(f"Cached verified blob: {digest[:12]}", flush=True)
        return target
    for attempt in range(1, 9):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            if sha256(partial) == digest:
                partial.replace(target)
                return target
            partial.unlink()
            offset = 0
        if offset > size:
            partial.unlink()
            offset = 0
        headers = {"Authorization": "Bearer " + token}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(BASE + "blobs/" + descriptor["digest"], headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise ValueError("Unexpected download range")
                else:
                    offset = 0  # Server ignored Range; restart this blob safely.
                with partial.open("ab" if offset else "wb") as stream:
                    current, reported_at = offset, time.monotonic()
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                        current += len(chunk)
                        if time.monotonic() - reported_at >= 10:
                            print(f"Downloading {digest[:12]}: {current / 1e6:.1f}/{size / 1e6:.1f} MB", flush=True)
                            reported_at = time.monotonic()
            if partial.stat().st_size != size or sha256(partial) != digest:
                raise ValueError("Incomplete blob or checksum mismatch")
            partial.replace(target)
            print(f"Verified blob: {digest[:12]} ({size / 1e6:.1f} MB)", flush=True)
            return target
        except Exception as error:
            # Do not log signed download URLs or bearer tokens.
            print(f"Download retry {attempt}/8 for {digest[:12]}: {type(error).__name__}", flush=True)
            if attempt == 8:
                raise RuntimeError(f"Could not download blob {digest[:12]}") from None
            time.sleep(min(attempt, 5))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    CACHE.mkdir(parents=True, exist_ok=True)
    arch = subprocess.check_output(["docker", "version", "--format", "{{.Server.Arch}}"], text=True).strip()
    token = fetch_json("https://auth.docker.io/token?" + urlencode({
        "service": "registry.docker.io", "scope": f"repository:{REPOSITORY}:pull",
    }))["token"]
    headers = {"Authorization": "Bearer " + token, "Accept": ACCEPT}
    index = fetch_json(BASE + "manifests/" + TAG, headers)
    if "manifests" in index:
        descriptor = next(m for m in index["manifests"] if
                          m.get("platform", {}).get("os") == "linux" and
                          m.get("platform", {}).get("architecture") == arch)
        manifest = fetch_json(BASE + "manifests/" + descriptor["digest"], headers)
    else:
        manifest = index
    config_path = download_blob(manifest["config"], token)
    config = json.loads(config_path.read_bytes())
    if config.get("os") != "linux" or config.get("architecture") != arch:
        raise ValueError("Image platform does not match Docker server")
    diff_ids = config["rootfs"]["diff_ids"]
    if len(diff_ids) != len(manifest["layers"]):
        raise ValueError("Layer count does not match image config")
    print(f"Official image: {REPOSITORY}:{TAG}; {len(diff_ids)} layers; linux/{arch}", flush=True)
    layer_paths = []
    for descriptor, diff_id in zip(manifest["layers"], diff_ids):
        blob = download_blob(descriptor, token)
        raw_digest = digest_hex(diff_id)
        layer_path = CACHE / (raw_digest + ".layer.tar")
        if not layer_path.exists() or sha256(layer_path) != raw_digest:
            temporary = layer_path.with_suffix(".tmp")
            if descriptor["mediaType"].endswith("+gzip"):
                source = gzip.open(blob, "rb")
            elif descriptor["mediaType"].endswith(".tar"):
                source = blob.open("rb")
            else:
                raise ValueError("Unsupported layer compression")
            digest = hashlib.sha256()
            with source, temporary.open("wb") as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    destination.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != raw_digest:
                raise ValueError("Uncompressed layer checksum mismatch")
            temporary.replace(layer_path)
        layer_paths.append(layer_path)
        print(f"Verified uncompressed layer: {raw_digest[:12]}", flush=True)
    archive = CACHE / "metabase-image.tar"
    config_name = digest_hex(manifest["config"]["digest"]) + ".json"
    manifest_bytes = json.dumps([{
        "Config": config_name, "RepoTags": [f"{REPOSITORY}:{TAG}"],
        "Layers": [layer.name for layer in layer_paths],
    }]).encode("utf-8")
    print("Building Docker image archive...", flush=True)
    with tarfile.open(archive, "w") as output:
        output.add(config_path, arcname=config_name)
        for layer in layer_paths:
            output.add(layer, arcname=layer.name)
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        output.addfile(info, io.BytesIO(manifest_bytes))
    print("Importing verified image into Docker...", flush=True)
    subprocess.run(["docker", "load", "--input", str(archive)], check=True)
    print("IMAGE_READY. Run: docker compose up -d --pull never metabase", flush=True)


if __name__ == "__main__":
    main()
