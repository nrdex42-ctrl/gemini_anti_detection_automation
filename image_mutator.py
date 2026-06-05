"""
ImageMutator - Perceptual hash evasion through fingerprint mutation.

Applies subtle, visually imperceptible transformations to images
so that each upload produces a unique perceptual hash while
maintaining visual quality.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.warning("ImageMutator: Pillow not installed; mutation disabled")


@dataclass
class MutationConfig:
    """Controls the type and intensity of mutations."""
    # Dimension mutations
    resize_jitter_px: int = 1          # ±1 pixel resize
    resize_jitter_enabled: bool = True

    # Quality mutations (JPEG only)
    quality_base: int = 92
    quality_jitter: int = 4            # ±4 quality
    quality_jitter_enabled: bool = True

    # Compression artifact injection
    recompress_count: int = 1          # Re-encode N times
    recompress_enabled: bool = True

    # Metadata stripping
    strip_exif: bool = True
    strip_icc: bool = True
    strip_all_metadata: bool = False

    # Subtle pixel noise
    noise_enabled: bool = True
    noise_max_delta: int = 2           # RGB channel shift ±2

    # Subtle color shift
    color_shift_enabled: bool = True
    color_shift_max: float = 1.5       # ±1.5 per channel

    # Very subtle sharpen/blur cycle
    sharpen_blur_cycle: bool = False

    # PNG-specific: add invisible pixel noise in alpha channel
    png_alpha_noise: bool = False

    # Seed control
    deterministic_seed: Optional[int] = None


class ImageMutator:
    """
    Applies visually imperceptible mutations to images so each
    instance has a unique perceptual fingerprint.

    All mutations are designed to be below the threshold of
    human perception while producing different hash values.
    """

    def __init__(self, config: Optional[MutationConfig] = None):
        self.config = config or MutationConfig()
        self._mutation_count = 0

    def mutate_file(
        self,
        input_path: str,
        output_path: str,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Mutate an image file and save to output_path.

        Returns metadata about what was changed.
        """
        if not HAS_PIL:
            return {"success": False, "error": "Pillow not installed"}

        img = Image.open(input_path)
        original_format = img.format or "PNG"
        target_format = self._normalize_format(format or original_format)

        result = self.mutate_image(img, target_format)
        img = result.pop("img")

        save_kwargs: Dict[str, Any] = {}
        if target_format == "JPEG":
            img = self._jpeg_ready_image(img)
            save_kwargs.update(self._jpeg_save_kwargs(result["quality"]))
        elif target_format == "PNG":
            save_kwargs["optimize"] = True
        elif target_format == "WEBP":
            save_kwargs["quality"] = result["quality"]
            save_kwargs["lossless"] = False

        img.save(output_path, format=target_format, **save_kwargs)

        # Verify the output
        output_size = os.path.getsize(output_path)
        result["output_path"] = output_path
        result["output_size"] = output_size
        result["output_format"] = target_format
        result["success"] = True

        self._mutation_count += 1
        return result

    def mutate_bytes(
        self,
        image_data: bytes,
        target_format: str = "JPEG",
    ) -> Tuple[bytes, Dict[str, Any]]:
        """
        Mutate image bytes and return the mutated bytes.

        Returns (mutated_bytes, mutation_metadata).
        """
        if not HAS_PIL:
            return image_data, {"success": False, "error": "Pillow not installed"}

        target_format = self._normalize_format(target_format)
        img = Image.open(io.BytesIO(image_data))
        result = self.mutate_image(img, target_format)
        img = result.pop("img")

        buffer = io.BytesIO()
        save_kwargs: Dict[str, Any] = {}
        if target_format == "JPEG":
            img = self._jpeg_ready_image(img)
            save_kwargs.update(self._jpeg_save_kwargs(result["quality"]))
        elif target_format == "PNG":
            save_kwargs["optimize"] = True
        elif target_format == "WEBP":
            save_kwargs["quality"] = result["quality"]

        img.save(buffer, format=target_format, **save_kwargs)
        mutated_data = buffer.getvalue()

        result["output_size"] = len(mutated_data)
        result["output_format"] = target_format
        result["success"] = True

        self._mutation_count += 1
        return mutated_data, result

    def mutate_image(
        self,
        img: "Image.Image",
        target_format: str = "JPEG",
    ) -> Dict[str, Any]:
        """
        Apply mutations to a PIL Image in-place.

        Returns metadata about applied mutations.
        """
        rng = random.Random(self.config.deterministic_seed)
        mutations: List[str] = []

        original_width, original_height = img.size

        # ── 1. Strip metadata ──
        if self.config.strip_exif or self.config.strip_all_metadata:
            exif_data = img.getexif() if hasattr(img, "getexif") else None
            img.info.clear()
            if exif_data and exif_data.get(271):  # Make tag
                mutations.append(f"stripped_exif(make={exif_data.get(271)})")
            else:
                mutations.append("stripped_metadata")

        # ── 2. Resize jitter (±1px) ──
        if self.config.resize_jitter_enabled and self.config.resize_jitter_px > 0:
            dx = rng.randint(-self.config.resize_jitter_px, self.config.resize_jitter_px)
            dy = rng.randint(-self.config.resize_jitter_px, self.config.resize_jitter_px)
            if dx != 0 or dy != 0:
                new_w = max(1, original_width + dx)
                new_h = max(1, original_height + dy)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                mutations.append(f"resize({original_width}x{original_height}→{new_w}x{new_h})")

        # ── 3. Subtle pixel noise ──
        if self.config.noise_enabled and self.config.noise_max_delta > 0:
            self._apply_pixel_noise(img, rng)
            mutations.append(f"pixel_noise(±{self.config.noise_max_delta})")

        # ── 4. Subtle color shift ──
        if self.config.color_shift_enabled and self.config.color_shift_max > 0:
            shift = self._apply_color_shift(img, rng)
            mutations.append(f"color_shift({shift})")

        # ── 5. Sharpen/blur cycle ──
        if self.config.sharpen_blur_cycle:
            img = img.filter(ImageFilter.SHARPEN)
            img = img.filter(ImageFilter.GaussianBlur(radius=0.3))
            mutations.append("sharpen_blur_cycle")

        # ── 6. Re-compression ──
        quality = self.config.quality_base
        if self.config.quality_jitter_enabled and self.config.quality_jitter > 0:
            quality += rng.randint(-self.config.quality_jitter, self.config.quality_jitter)
            quality = max(50, min(99, quality))

        if self.config.recompress_enabled and self.config.recompress_count > 1:
            for i in range(self.config.recompress_count - 1):
                buffer = io.BytesIO()
                if target_format == "JPEG" and img.mode == "RGBA":
                    temp = img.convert("RGB")
                    temp.save(buffer, format="JPEG", **self._jpeg_save_kwargs(quality))
                else:
                    if target_format == "JPEG":
                        img = self._jpeg_ready_image(img)
                        img.save(buffer, format=target_format, **self._jpeg_save_kwargs(quality))
                    else:
                        img.save(buffer, format=target_format, quality=quality)
                buffer.seek(0)
                img = Image.open(buffer)
            mutations.append(f"recompress({self.config.recompress_count}x)")

        return {
            "mutations": mutations,
            "quality": quality,
            "final_size": img.size,
            "format": target_format,
            "img": img,
        }

    @staticmethod
    def _normalize_format(format_name: str) -> str:
        target_format = (format_name or "JPEG").upper()
        return "JPEG" if target_format == "JPG" else target_format

    @staticmethod
    def _jpeg_ready_image(img: "Image.Image") -> "Image.Image":
        if img.mode == "RGB":
            return img
        if img.mode in {"RGBA", "LA"}:
            rgba = img.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            return flattened
        return img.convert("RGB")

    @staticmethod
    def _jpeg_save_kwargs(quality: int) -> Dict[str, Any]:
        return {
            "quality": quality,
            "optimize": True,
            "progressive": False,
            "subsampling": 2,
        }

    def _apply_pixel_noise(self, img: "Image.Image", rng: random.Random):
        """Add imperceptible noise to random pixels."""
        max_delta = self.config.noise_max_delta
        # Only mutate ~5% of pixels to keep changes invisible
        pixels = img.load()
        width, height = img.size

        if img.mode == "RGBA":
            for _ in range(max(1, (width * height) // 20)):
                x = rng.randint(0, width - 1)
                y = rng.randint(0, height - 1)
                r, g, b, a = pixels[x, y]
                r = max(0, min(255, r + rng.randint(-max_delta, max_delta)))
                g = max(0, min(255, g + rng.randint(-max_delta, max_delta)))
                b = max(0, min(255, b + rng.randint(-max_delta, max_delta)))
                pixels[x, y] = (r, g, b, a)
        elif img.mode == "RGB":
            for _ in range(max(1, (width * height) // 20)):
                x = rng.randint(0, width - 1)
                y = rng.randint(0, height - 1)
                r, g, b = pixels[x, y]
                r = max(0, min(255, r + rng.randint(-max_delta, max_delta)))
                g = max(0, min(255, g + rng.randint(-max_delta, max_delta)))
                b = max(0, min(255, b + rng.randint(-max_delta, max_delta)))
                pixels[x, y] = (r, g, b)

    def _apply_color_shift(self, img: "Image.Image", rng: random.Random) -> str:
        """Apply a very subtle global color shift."""
        max_shift = self.config.color_shift_max
        r_shift = rng.uniform(-max_shift, max_shift)
        g_shift = rng.uniform(-max_shift, max_shift)
        b_shift = rng.uniform(-max_shift, max_shift)

        if img.mode in ("RGB", "RGBA"):
            # Split channels, shift, merge
            channels = list(img.split())
            if len(channels) >= 3:
                channels[0] = channels[0].point(lambda x: max(0, min(255, int(x + r_shift))))
                channels[1] = channels[1].point(lambda x: max(0, min(255, int(x + g_shift))))
                channels[2] = channels[2].point(lambda x: max(0, min(255, int(x + b_shift))))
                merged = Image.merge(img.mode, channels)
                img.paste(merged)

        return f"r={r_shift:+.1f} g={g_shift:+.1f} b={b_shift:+.1f}"

    @staticmethod
    def perceptual_hash(image_data: bytes) -> str:
        """
        Compute a simple difference hash (dHash) for verification.
        Not cryptographically secure, just for testing mutation effect.
        """
        if not HAS_PIL:
            return hashlib.md5(image_data).hexdigest()

        img = Image.open(io.BytesIO(image_data)).convert("L")
        img = img.resize((9, 8), Image.LANCZOS)
        pixels = list(img.getdata())
        hash_bits = []
        for y in range(8):
            for x in range(8):
                left = pixels[y * 9 + x]
                right = pixels[y * 9 + x + 1]
                hash_bits.append(1 if left > right else 0)

        hash_int = 0
        for bit in hash_bits:
            hash_int = (hash_int << 1) | bit
        return f"{hash_int:016x}"


def mutate_image_for_upload(
    image_data: bytes,
    target_format: str = "JPEG",
    seed: Optional[int] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Convenience function: mutate image bytes for a single upload.

    Usage:
        mutated_data, info = mutate_image_for_upload(original_bytes)
        # Upload mutated_data instead of original_bytes
    """
    config = MutationConfig(
        deterministic_seed=seed,
        resize_jitter_enabled=True,
        noise_enabled=True,
        color_shift_enabled=True,
        strip_exif=True,
        quality_jitter_enabled=True,
    )
    mutator = ImageMutator(config)
    return mutator.mutate_bytes(image_data, target_format)
