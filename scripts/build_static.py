"""Pre-generate downsampled inline base maps (plan Step 4).

Reads the full-res floor JPGs, downsamples to 0.25x with LANCZOS, writes
WebP q65 into src/static/. Run once at Docker build time (and locally
before Step 5 template work). Outputs are gitignored build artifacts --
never compute LANCZOS per request (slower than JPEG-encoding the full map).
"""

import os
from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, 'src', 'static')

JOBS = (
    ('5f_base.jpg', '5f_small.webp'),
    ('6f_base.jpg', '6f_small.webp'),
)


def main():
    os.makedirs(STATIC_DIR, exist_ok=True)
    for src_name, out_name in JOBS:
        src = os.path.join(BASE_DIR, src_name)
        out = os.path.join(STATIC_DIR, out_name)
        with Image.open(src) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            small = img.resize((img.width // 4, img.height // 4),
                               Image.LANCZOS)
            small.save(out, 'WEBP', quality=65)
            size = os.path.getsize(out)
        print(f'{out_name}: {small.size[0]}x{small.size[1]}, {size} bytes')


if __name__ == '__main__':
    main()
