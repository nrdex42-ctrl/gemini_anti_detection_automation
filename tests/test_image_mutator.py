import io
from pathlib import Path

from PIL import Image

from fb_automation.image_mutator import ImageMutator, MutationConfig
from fb_automation.live_emulation import facebook_jpeg_structure_error, save_facebook_safe_jpeg


def test_mutate_bytes_uses_mutated_image_and_writes_baseline_jpeg():
    source = Image.new('RGB', (120, 120), (30, 80, 120))
    source_buffer = io.BytesIO()
    source.save(source_buffer, 'PNG')

    config = MutationConfig(
        deterministic_seed=1,
        resize_jitter_enabled=True,
        resize_jitter_px=1,
        noise_enabled=False,
        color_shift_enabled=False,
        recompress_enabled=False,
    )
    mutator = ImageMutator(config)
    mutated_bytes, info = mutator.mutate_bytes(source_buffer.getvalue(), 'JPEG')

    assert info['final_size'] == (119, 121)
    assert 'img' not in info
    assert facebook_jpeg_structure_error(mutated_bytes) == ''
    with Image.open(io.BytesIO(mutated_bytes)) as mutated:
        assert mutated.size == (119, 121)
        assert mutated.mode == 'RGB'


def test_mutate_file_converts_rgba_before_jpeg_save(tmp_path: Path):
    source_path = tmp_path / 'source.png'
    output_path = tmp_path / 'mutated.jpg'
    Image.new('RGBA', (140, 140), (30, 80, 120, 128)).save(source_path, 'PNG')

    config = MutationConfig(
        deterministic_seed=1,
        noise_enabled=False,
        color_shift_enabled=False,
        recompress_enabled=False,
    )
    info = ImageMutator(config).mutate_file(str(source_path), str(output_path), 'JPEG')

    assert info['success'] is True
    assert facebook_jpeg_structure_error(output_path.read_bytes()) == ''
    with Image.open(output_path) as output:
        assert output.mode == 'RGB'


def test_save_facebook_safe_jpeg_converts_custom_png_to_valid_baseline(tmp_path: Path):
    source_path = tmp_path / 'source.png'
    target_path = tmp_path / 'safe.jpg'
    Image.new('RGBA', (180, 180), (180, 40, 30, 120)).save(source_path, 'PNG')

    metadata = save_facebook_safe_jpeg(source_path, target_path)

    assert metadata['source_format'] == 'PNG'
    assert metadata['output_format'] == 'JPEG'
    assert facebook_jpeg_structure_error(target_path.read_bytes()) == ''
    with Image.open(target_path) as output:
        assert output.mode == 'RGB'
