"""Find a stable webcam inset with the configured local vision model."""
from __future__ import annotations

import base64
import json
import threading
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Optional, Tuple

import requests

from . import local_ai, render

Box = Tuple[float, float, float, float]
SCHEMA = {"type": "object", "properties": {"box": {"type": ["array", "null"],
          "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}}, "required": ["box"]}


def frame_box(frame: bytes, settings) -> Optional[Box]:
    prompt = ("Locate a small, clearly bounded webcam inset of the presenter overlaid on another scene. "
              "Give the inset's outer rectangle, NOT the face or body. Ignore HUD, subtitles, people in "
              "the main scene, unbounded cutouts and full-screen camera shots. If no clear rectangular "
              "webcam inset exists, return box:null. Otherwise return box:[left,top,right,bottom] in "
              "coordinates normalized independently on both axes from 0 to 1000.")
    with local_ai._LOCK, requests.Session() as session:
        session.trust_env = False
        response = session.post(f"{settings.local_url}/chat/completions", json={
            "model": settings.local_vision_model, "temperature": 0, "max_tokens": 100,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "facecam", "strict": True, "schema": SCHEMA}},
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                 + base64.b64encode(frame).decode("ascii")}}]}],
        }, timeout=(3, 120), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"Ollama returned HTTP {response.status_code}")
        box = json.loads(response.json()["choices"][0]["message"]["content"])["box"]
    if box is None:
        return None
    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
        raise ValueError("invalid facecam rectangle from Ollama")
    x, y, r, b = [v / 1000 for v in box]
    if not (0 <= x < r <= 1 and 0 <= y < b <= 1 and 0.06 <= r-x <= 0.48 and 0.06 <= b-y <= 0.58):
        return None
    return x, y, r, b


def box_iou(a: Box, b: Box) -> float:
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - overlap
    return overlap / union


def consensus(boxes: list[Optional[Box]]) -> Optional[Box]:
    present = [box for box in boxes if box is not None]
    for n in range(len(present), max(3, len(boxes)//2 + 1)-1, -1):
        for group in combinations(present, n):
            if all(box_iou(a, b) >= 0.68 for a, b in combinations(group, 2)):
                x, y, r, b = (median(values) for values in zip(*group))
                return x, y, r-x, b-y
    return None


class SourceFacecam:
    def __init__(self, *, reject_moved: bool = False):
        self.lock = threading.Lock()
        self.done = False
        self.box: Optional[Box] = None
        self.reject_moved = reject_moved

    def resolve(self, samples: list[tuple[Path, float]], settings, log) -> Optional[Box]:
        with self.lock:
            if self.done or not settings.local_vision_model:
                return self.box
            boxes = []
            seen = set()
            try:
                for path, at in samples[:6]:
                    frames = render.extract_frames(path, [at], width=448, timeout=12)
                    if not frames or frames[0] in seen:
                        continue
                    seen.add(frames[0])
                    boxes.append(frame_box(frames[0], settings))
                    if self.reject_moved:
                        continue
                    if len(boxes) == 3 and not any(boxes):
                        break
                    candidate = consensus(boxes)
                    if candidate is not None:
                        self.box = candidate
                        break
            except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
                log(f"facecam: local detection failed: {exc}")
            else:
                if self.reject_moved:
                    detected = [box for box in boxes if box is not None]
                    candidate = consensus(detected)
                    if candidate is not None:
                        x, y, w, h = candidate
                        bounds = (x, y, x + w, y + h)
                        if any(box_iou(bounds, box) < 0.20 for box in detected):
                            log("facecam: conflicting inset positions within the Short")
                        else:
                            self.box = candidate
            self.done = bool(boxes)
            found = sum(box is not None for box in boxes)
            log(f"facecam: {'stable inset detected' if self.box else 'no stable inset; using blurred layout'} "
                f"({found}/{len(boxes)} frames with an inset candidate)")
            return self.box
