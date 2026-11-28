"""The streamer's webcam in a stream clip: where it is, so the clip can show the streamer's face above the game.

1. A face that stays in one place across ten frames is the webcam (faces in the game move; a webcam face is small).
2. Around it, the webcam's edges: the nearest straight line that lasts in every frame with moving game beyond it.
3. The part of it that fits the top panel's shape.

Run on its own (`python3 -m worker.facecam <clip> <start> <end> <w> <h> <ffmpeg> <aspect>`, prints JSON) by
worker/clips.py, so OpenCV stays out of the worker's own process.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

CAM_SAMPLE_W = 1280                     # frames are looked at this wide; boxes are scaled back to the clip's size


def _gray_frames(src: Path, start: float, end: float, info: dict, n: int = 10) -> list:
    ffmpeg = info.get("ffmpeg") or "ffmpeg"
    import numpy as np
    sw, sh = CAM_SAMPLE_W, int(round(info["h"] * CAM_SAMPLE_W / info["w"] / 2) * 2)
    out = []
    for i in range(n):
        t = start + (end - start) * (i + .5) / n
        raw = subprocess.run([ffmpeg, "-loglevel", "quiet", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                              "-vf", f"scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, timeout=60).stdout
        if len(raw) == sw * sh:
            out.append(np.frombuffer(raw, np.uint8).reshape(sh, sw))
    return out


def steady_face(grays: list) -> Optional[tuple[float, float, float]]:
    """(x, y, size) of a face that stays in one place across the frames: the webcam. Faces in the game move about
    (and a webcam face is small: under a tenth of the frame's width)."""
    import cv2
    import numpy as np
    front, side = (cv2.CascadeClassifier(cv2.data.haarcascades + n)
                   for n in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"))
    W = grays[0].shape[1]
    big = (int(W * .1), int(W * .1))
    dets = []
    for fi, g in enumerate(grays):
        g = cv2.equalizeHist(g)
        for c, flip in ((front, False), (side, False), (side, True)):    # profile faces looking either way
            img = cv2.flip(g, 1) if flip else g
            for (x, y, w, h) in c.detectMultiScale(img, 1.1, 5, minSize=(24, 24), maxSize=big):
                dets.append((fi, (W - x - w if flip else x) + w / 2, y + h / 2, float(w)))
    best, hits = None, 0
    for (_, cx, cy, s) in dets:
        near = [d for d in dets if abs(d[1] - cx) < s * .6 and abs(d[2] - cy) < s * .6 and .6 < d[3] / s < 1.6]
        n = len({d[0] for d in near})
        if n > hits:
            hits = n
            best = tuple(float(np.median([d[k] for d in near])) for k in (1, 2, 3))
    return best if best and hits >= max(3, len(grays) * .3) else None


def webcam_box(grays: list, face: tuple) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the webcam: around the face, the nearest straight edge that stays put in every frame with
    moving game on its far side (a line in the streamer's room has the still room on both sides). The frame's
    edge when the webcam sits in a corner; a usual webcam's size when no edge shows."""
    import numpy as np
    cx, cy, s = face
    H, W = grays[0].shape
    stack = np.stack([g.astype(np.float32) for g in grays])
    gx = np.median(np.abs(np.diff(stack, axis=2)), axis=0)      # edges between columns that last
    gy = np.median(np.abs(np.diff(stack, axis=1)), axis=0)      # edges between rows that last
    act = np.median(np.abs(np.diff(stack, axis=0)), axis=0)     # how much each pixel changes frame to frame
    band = max(4, int(s * .25))

    def edge(along, cut, v, lo, hi, step):
        line = along[lo:hi, min(v, W - 2)] if cut else along[min(v, H - 2), lo:hi]
        if not line.size or (line > 12).mean() < .7:
            return False
        far, near = ((v - band, v - 1), (v + 2, v + band)) if step < 0 else ((v + 2, v + band), (v - band, v - 1))
        pick = (lambda a, b: act[lo:hi, max(0, a):max(0, b)]) if cut else (lambda a, b: act[max(0, a):max(0, b), lo:hi])
        o, i = pick(*far), pick(*near)
        return o.size > 0 and i.size > 0 and o.mean() > i.mean() + 2.5

    def side(along, cut, centre, near, far, step, limit, guess, lo, hi):
        for d in range(int(near), int(far)):
            v = int(centre + step * d)
            if v <= 0 or v >= limit - 1:
                return 0 if step < 0 else limit
            if edge(along, cut, v, lo, hi, step):
                return v if step < 0 else v + 1
        return int(max(0, min(limit, centre + step * guess)))

    ys0, ys1 = max(0, int(cy - s * .6)), min(H, int(cy + s * .6))
    left = side(gx, True, cx, s * .9, s * 4.5, -1, W, s * 2.4, ys0, ys1)
    right = side(gx, True, cx, s * .9, s * 4.5, 1, W, s * 2.4, ys0, ys1)
    xs0, xs1 = max(0, int(max(left, cx - s))), min(W, int(min(right, cx + s)))
    top = side(gy, False, cy, s * .9, s * 3.5, -1, H, s * 1.5, xs0, xs1)
    bottom = side(gy, False, cy, s * .7, s * 3.5, 1, H, s * 1.6, xs0, xs1)
    return left, top, right - left, bottom - top


def cam_crop(box: tuple, face: tuple, aspect: float) -> tuple[int, int, int, int]:
    """The part of the webcam shown in the top panel: as much of it as fits the panel's shape, face a bit above
    the middle. A couple of pixels in, so the webcam's border line doesn't show."""
    bx, by, bw, bh = box[0] + 2, box[1] + 2, box[2] - 4, box[3] - 4
    if bw / bh >= aspect:
        h, w = bh, bh * aspect
        x, y = min(max(face[0] - w / 2, bx), bx + bw - w), by
    else:
        w, h = bw, bw / aspect
        x, y = bx, min(max(face[1] - h * .42, by), by + bh - h)
    ev = lambda v: int(v) // 2 * 2
    return ev(x), ev(y), ev(w), ev(h)


def find(src: Path, start: float, end: float, info: dict, aspect: float) -> Optional[tuple[int, int, int, int]]:
    """Where to cut the streamer's webcam from (x, y, w, h in the clip's pixels), or None when there's no webcam
    (or it can't be told apart from the game)."""
    grays = _gray_frames(src, start, end, info)
    face = steady_face(grays) if len(grays) >= 5 else None
    if not face:
        return None
    H0, W0 = grays[0].shape
    x, y, w, h = webcam_box(grays, face)
    if w < face[2] * 1.8 or h < face[2] * 1.4 or w * h > .25 * W0 * H0:   # not a webcam's shape: leave the layout alone
        return None
    k = info["w"] / W0
    cx, cy, cw, ch = cam_crop((x, y, w, h), face, aspect)
    ev = lambda v: int(v * k) // 2 * 2
    return ev(cx), ev(cy), max(2, ev(cw)), max(2, ev(ch))


def main(argv: list[str]) -> int:
    src, start, end, w, h, ffmpeg, aspect = argv
    box = find(Path(src), float(start), float(end), {"w": int(w), "h": int(h), "ffmpeg": ffmpeg}, float(aspect))
    print(json.dumps(list(box) if box else None))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
