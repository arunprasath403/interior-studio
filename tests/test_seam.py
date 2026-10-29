import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from generator import seam


def seamless(h=128, w=512, seed=0):
    """A texture that wraps around exactly: sums of sinusoids with whole numbers of cycles across the width."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.zeros((h, w, 3), dtype=np.float32) + 128
    for k in (2, 3, 5):
        phase = rng.uniform(0, 6.28)
        img += 25 * np.sin(2 * np.pi * k * x / w + phase)[..., None] * np.array([1.0, 0.8, 0.6])
    img += 20 * np.sin(2 * np.pi * y / 32)[..., None]
    return np.clip(img, 0, 255).astype(np.uint8)


def with_brightness_step(img, step=40):
    """Brightness ramps up from left to right, so the right edge no longer meets the left edge."""
    ramp = np.linspace(0, step, img.shape[1], dtype=np.float32)[None, :, None]
    return np.clip(img.astype(np.float32) + ramp, 0, 255).astype(np.uint8)


class SeamScoreTests(unittest.TestCase):
    def test_a_wrapping_texture_scores_close_to_one(self):
        self.assertLess(seam.seam_score(seamless()), 1.5)

    def test_a_brightness_step_scores_high(self):
        self.assertGreater(seam.seam_score(with_brightness_step(seamless())), seam.FLAG_SCORE)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.broken = with_brightness_step(seamless())

    def test_repair_removes_the_step(self):
        fixed = seam.repair_seam(self.broken)
        self.assertLess(seam.seam_score(fixed), seam.FLAG_SCORE)
        self.assertLess(seam.seam_score(fixed), seam.seam_score(self.broken) / 2)

    def test_repair_keeps_shape_and_dtype(self):
        fixed = seam.repair_seam(self.broken)
        self.assertEqual(fixed.shape, self.broken.shape)
        self.assertEqual(fixed.dtype, self.broken.dtype)

    def test_pixels_away_from_the_seam_are_untouched(self):
        fixed = seam.repair_seam(self.broken, band=64)
        w = self.broken.shape[1]
        # the seam is at the left/right edge, so only the outer 64 columns on each side may change
        self.assertTrue(np.array_equal(fixed[:, 64:w - 64], self.broken[:, 64:w - 64]))

    def test_repair_does_not_modify_its_input(self):
        before = self.broken.copy()
        seam.repair_seam(self.broken)
        self.assertTrue(np.array_equal(before, self.broken))

    def test_a_clean_image_is_barely_changed(self):
        clean = seamless()
        fixed = seam.repair_seam(clean)
        self.assertLess(np.abs(fixed.astype(int) - clean.astype(int)).mean(), 2.0)

    def test_float_images_stay_float(self):
        fixed = seam.repair_seam(self.broken.astype(np.float32))
        self.assertEqual(fixed.dtype, np.float32)

    def test_repair_is_deterministic(self):
        self.assertTrue(np.array_equal(seam.repair_seam(self.broken), seam.repair_seam(self.broken)))


class VerticalMisalignmentTests(unittest.TestCase):
    def test_a_shifted_structure_is_detected(self):
        h, w = 128, 512
        stripes = (128 + 60 * np.sin(2 * np.pi * np.arange(h) / 20)).astype(np.float32)
        img = np.zeros((h, w, 3), dtype=np.float32)
        img[:, : w // 2] = stripes[:, None, None]
        img[:, w // 2:] = np.roll(stripes, 6)[:, None, None]  # right half sits 6 rows lower than the left half
        rolled = np.roll(img, w // 2, axis=1)  # the wrap-around seam is now in the middle
        self.assertIn(abs(seam._estimate_shift(rolled, w // 2)), (5, 6, 7))

    def test_aligned_structure_gives_no_shift(self):
        h, w = 128, 512
        stripes = (128 + 60 * np.sin(2 * np.pi * np.arange(h) / 20)).astype(np.float32)
        img = np.tile(stripes[:, None, None], (1, w, 3))
        self.assertEqual(seam._estimate_shift(img, w // 2), 0)


class FileTests(unittest.TestCase):
    def test_repair_file_fixes_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pano.jpg"
            Image.fromarray(with_brightness_step(seamless())).save(path, quality=98)
            result = seam.repair_file(path)
            self.assertTrue(result["repaired"])
            self.assertLess(result["after"], result["before"])

    def test_clean_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pano.jpg"
            Image.fromarray(seamless()).save(path, quality=98)
            before = path.read_bytes()
            self.assertFalse(seam.repair_file(path)["repaired"])
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
