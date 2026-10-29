"""Detect and soften the left-right wrap-around seam of an equirectangular panorama.

DiT360 uses circular padding, but its panoramas still show a small discontinuity where the right edge meets the
left edge. ``seam_score`` measures it and ``repair_seam`` hides it with classical image processing on the CPU:

1. roll the image so the seam is in the middle,
2. remove the brightness/colour step across it with a smooth ramp,
3. align horizontal structure (shelves, counters) with a small progressive vertical shear,
4. blur a very narrow strip to hide what remains.

This can hide steps, shifts and texture changes of a few pixels. It cannot invent missing content, so a seam where
objects genuinely do not line up needs diffusion inpainting instead (not built).
"""
from __future__ import annotations

import numpy as np

BAND = 96        # columns on each side of the seam that the repair may modify
MAX_SHIFT = 12   # largest vertical misalignment (pixels) the shear step will try to correct
FEATHER = 5      # half-width of the final smoothing strip
FLAG_SCORE = 3.0  # seam_score above this suggests a visible seam (see tests for the calibration)


def _prediction_error(a: np.ndarray, c: int) -> float:
    """How badly column c is predicted by extrapolating the two columns before it."""
    return float(np.abs(a[:, c] - (2 * a[:, c - 1] - a[:, c - 2])).mean())


def seam_score(image: np.ndarray, window: int = 300) -> float:
    """Seam discontinuity relative to ordinary columns nearby. About 1 means no seam; higher means a visible one.

    Both halves of the image are rolled so the seam sits in the middle, then the seam's extrapolation error is
    divided by the median error of the surrounding columns.
    """
    a = np.asarray(image, dtype=np.float32)
    w = a.shape[1]
    rolled, c = np.roll(a, w // 2, axis=1), w // 2
    window = min(window, c - 4)
    others = [_prediction_error(rolled, x) for x in range(c - window, c + window) if abs(x - c) > 3]
    return _prediction_error(rolled, c) / max(float(np.median(others)), 1e-6)


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _smooth_vertical(v: np.ndarray, sigma: float = 6.0) -> np.ndarray:
    """Gaussian-smooth along axis 0 (rows) so a correction follows broad structure, not pixel noise."""
    radius = int(3 * sigma)
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    kernel /= kernel.sum()
    pad = np.pad(v, ((radius, radius),) + ((0, 0),) * (v.ndim - 1), mode="edge")
    return np.stack([np.convolve(pad[:, i], kernel, mode="valid") for i in range(v.shape[1])], axis=1)


def _estimate_shift(a: np.ndarray, c: int, probe: int = 24) -> int:
    """Vertical offset (pixels, +down) that best aligns the right side's structure with the left side's."""
    lum = a.mean(axis=2)
    left = np.diff(lum[:, c - probe:c].mean(axis=1))
    right = np.diff(lum[:, c:c + probe].mean(axis=1))
    best, best_err = 0, float(np.abs(left - right).mean())
    for s in range(-MAX_SHIFT, MAX_SHIFT + 1):
        a_, b_ = (left[s:], right[:len(right) - s]) if s >= 0 else (left[:s], right[-s:])
        err = float(np.abs(a_ - b_).mean())
        if err < best_err * 0.9:  # require a clear improvement, so noise never triggers a shift
            best, best_err = s, err
    return best


def _shift_columns(a: np.ndarray, cols: slice, shifts: np.ndarray) -> None:
    """Shift each column in ``cols`` vertically by its own (fractional) amount, in place."""
    h = a.shape[0]
    rows = np.arange(h, dtype=np.float32)
    for i, x in enumerate(range(*cols.indices(a.shape[1]))):
        src = np.clip(rows - shifts[i], 0, h - 1)
        for ch in range(a.shape[2]):
            a[:, x, ch] = np.interp(src, rows, a[:, x, ch])


def repair_seam(image: np.ndarray, band: int = BAND) -> np.ndarray:
    """Return a copy of ``image`` (H x W x 3, uint8 or float) with the wrap-around seam softened."""
    original_dtype = np.asarray(image).dtype
    a = np.asarray(image, dtype=np.float32).copy()
    h, w, _ = a.shape
    a = np.roll(a, w // 2, axis=1)
    c = w // 2
    band = min(band, c - 4)
    ramp = np.arange(1, band + 1, dtype=np.float32) / band  # 0 -> 1 towards the seam

    # 1. Remove the colour/brightness step, using the jump left over after allowing for the local gradient.
    jump = (a[:, c] - a[:, c - 1]) - 0.5 * ((a[:, c - 1] - a[:, c - 2]) + (a[:, c + 1] - a[:, c]))
    jump = _smooth_vertical(jump)
    weight = _smoothstep(ramp)[None, :, None]
    a[:, c - band:c] += (jump[:, None, :] / 2) * weight
    a[:, c:c + band] -= (jump[:, None, :] / 2) * weight[:, ::-1]

    # 2. Align structure with a progressive vertical shear that is zero at the band edges.
    shift = _estimate_shift(a, c)
    if shift:
        amount = _smoothstep(ramp) * (shift / 2)
        _shift_columns(a, slice(c - band, c), amount)           # left side moves towards the right side
        _shift_columns(a, slice(c, c + band), -amount[::-1])    # right side moves towards the left side

    # 3. Smooth a very narrow strip so a small residual step is not visible.
    lo, hi = c - FEATHER, c + FEATHER
    strip = a[:, lo - FEATHER:hi + FEATHER].copy()
    kernel = np.ones(2 * FEATHER + 1, dtype=np.float32) / (2 * FEATHER + 1)
    blurred = np.stack([np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="same"), 1, strip[:, :, ch])
                        for ch in range(a.shape[2])], axis=2)
    mask = _smoothstep(1 - np.abs(np.arange(lo - FEATHER, hi + FEATHER) - c) / (FEATHER + 1e-6))[None, :, None]
    a[:, lo - FEATHER:hi + FEATHER] = strip * (1 - mask) + blurred * mask

    out = np.roll(a, -(w // 2), axis=1)
    if np.issubdtype(original_dtype, np.integer):
        out = np.clip(np.rint(out), 0, 255)
    return out.astype(original_dtype)


def repair_file(path, force: bool = False) -> dict:
    """Score a panorama file and, if it has a visible seam (or ``force``), repair it in place.

    Returns {"before": score, "after": score, "repaired": bool}.
    """
    from PIL import Image

    image = np.asarray(Image.open(path).convert("RGB"))
    before = seam_score(image)
    if before <= FLAG_SCORE and not force:
        return {"before": round(before, 2), "after": round(before, 2), "repaired": False}
    fixed = repair_seam(image)
    Image.fromarray(fixed).save(path, format="JPEG", quality=95)
    return {"before": round(before, 2), "after": round(seam_score(fixed), 2), "repaired": True}


def main() -> None:
    import argparse
    import shutil

    parser = argparse.ArgumentParser(description="Score and repair the wrap-around seam of a panorama.")
    parser.add_argument("image")
    parser.add_argument("output", nargs="?", help="write the repaired image here (default: only report the score)")
    parser.add_argument("--force", action="store_true", help="repair even if the score is below the threshold")
    args = parser.parse_args()

    if not args.output:
        from PIL import Image

        print(f"seam score {seam_score(np.asarray(Image.open(args.image).convert('RGB'))):.2f} "
              f"(visible seam suggested above {FLAG_SCORE})")
        return
    shutil.copyfile(args.image, args.output)
    print(repair_file(args.output, force=args.force))


if __name__ == "__main__":
    main()
