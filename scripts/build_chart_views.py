#!/usr/bin/env python3
"""Build question-independent whole-chart and color-isolated chart views."""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

BUILDER = "chart-view-builder"
BUILDER_VERSION = "0.1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hue_degrees(color: np.ndarray) -> float:
    hue, _saturation, _value = colorsys.rgb_to_hsv(*(color.astype(float) / 255.0))
    return hue * 360.0


def hue_distance(left: float, right: float) -> float:
    difference = abs(left - right)
    return min(difference, 360.0 - difference)


def discover_color_clusters(
    rgb: np.ndarray, minimum_pixels: int, maximum_series: int, minimum_relative_weight: float
) -> list[dict[str, Any]]:
    flat = rgb.reshape(-1, 3)
    channel_max = flat.max(axis=1)
    channel_min = flat.min(axis=1)
    chroma = channel_max - channel_min
    brightness = flat.mean(axis=1)
    saturation = chroma / np.maximum(channel_max, 1)
    colored = flat[(chroma >= 40) & (saturation >= 0.20) & (brightness >= 35) & (brightness <= 245)]
    if not len(colored):
        return []
    quantized = (colored // 32) * 32 + 16
    bins, counts = np.unique(quantized, axis=0, return_counts=True)
    order = np.argsort(counts)[::-1]
    clusters: list[dict[str, Any]] = []
    for bin_index in order:
        center = bins[bin_index].astype(float)
        hue = hue_degrees(center)
        count = int(counts[bin_index])
        if count < minimum_pixels:
            break
        existing = next((item for item in clusters if hue_distance(item["hue"], hue) <= 18), None)
        if existing is None:
            clusters.append({"center": center, "hue": hue, "members": [center], "weight": count})
        else:
            total = existing["weight"] + count
            existing["center"] = (existing["center"] * existing["weight"] + center * count) / total
            existing["hue"] = hue_degrees(existing["center"])
            existing["members"].append(center)
            existing["weight"] = total
    clusters.sort(key=lambda item: item["weight"], reverse=True)
    if not clusters:
        return []
    relative_floor = clusters[0]["weight"] * minimum_relative_weight
    return [item for item in clusters if item["weight"] >= relative_floor][:maximum_series]


def isolated_view(rgb: np.ndarray, centers: list[np.ndarray], tolerance: float) -> tuple[np.ndarray, int]:
    distances = [np.sqrt(np.square(rgb.astype(float) - center.reshape(1, 1, 3)).sum(axis=2)) for center in centers]
    distance = np.minimum.reduce(distances)
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    selected = (distance <= tolerance) & (chroma >= 25)
    gray = np.asarray(ImageOps.grayscale(Image.fromarray(rgb)), dtype=np.float32)
    muted = 246.0 - (255.0 - gray) * 0.18
    output = np.repeat(muted[:, :, None], 3, axis=2).astype(np.uint8)
    output[selected] = rgb[selected]
    return output, int(selected.sum())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-series", type=int, default=6)
    parser.add_argument("--minimum-pixels", type=int, default=24)
    parser.add_argument("--minimum-relative-weight", type=float, default=0.35)
    parser.add_argument("--tolerance", type=float, default=82.0)
    args = parser.parse_args()
    if args.max_series < 1 or args.minimum_pixels < 1 or not 0 < args.minimum_relative_weight <= 1 or args.tolerance <= 0:
        parser.error("series, pixel, and tolerance options must be positive")
    image_path = args.image.resolve()
    if not image_path.is_file():
        parser.error(f"image not found: {image_path}")
    args.out.mkdir(parents=True, exist_ok=True)
    rgb = np.asarray(Image.open(image_path).convert("RGB"))
    clusters = discover_color_clusters(
        rgb, args.minimum_pixels, args.max_series, args.minimum_relative_weight
    )

    whole_path = args.out / "view_00_whole.png"
    Image.fromarray(rgb).save(whole_path)
    views: list[dict[str, Any]] = [{
        "view_id": "whole", "kind": "whole", "path": whole_path.name,
        "sha256": sha256_file(whole_path), "selected_pixels": int(rgb.shape[0] * rgb.shape[1]),
    }]
    for index, cluster in enumerate(clusters, start=1):
        view, selected_pixels = isolated_view(rgb, cluster["members"], args.tolerance)
        view_path = args.out / f"view_{index:02d}_series.png"
        Image.fromarray(view).save(view_path)
        center = [int(round(value)) for value in cluster["center"].tolist()]
        views.append({
            "view_id": f"series_{index}",
            "kind": "color_isolated",
            "path": view_path.name,
            "sha256": sha256_file(view_path),
            "cluster_rgb": center,
            "cluster_hex": "#" + "".join(f"{value:02x}" for value in center),
            "cluster_weight": int(cluster["weight"]),
            "selected_pixels": selected_pixels,
        })
    manifest = {
        "schema_version": "0.1",
        "record_type": "chart_view_set",
        "source": {"path": str(image_path), "sha256": sha256_file(image_path)},
        "views": views,
        "provenance": {
            "builder": BUILDER,
            "builder_version": BUILDER_VERSION,
            "question_independent": True,
            "parameters": {
                "max_series": args.max_series,
                "minimum_pixels": args.minimum_pixels,
                "minimum_relative_weight": args.minimum_relative_weight,
                "tolerance": args.tolerance,
            },
        },
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(views) - 1} isolated view(s): {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
