#!/usr/bin/env python3
"""
add_caption_border.py

Adds a Polaroid-style white border with caption text to an image.
Uses PERCENTAGE-BASED dimensions for resolution independence.

All sizing is relative to the exported image dimensions:
- Border height: % of image height
- Font size: % of image height
- Padding: % of image width

This ensures consistent proportions whether exporting at 1000px or 6000px!
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont


def add_caption_border(
    image_path: str | os.PathLike[str],
    caption: str,
    border_percent: float = 5.0,     # % of image height
    font_percent: float = 1.5,       # % of image height
    padding_percent: float = 1.0,    # % of image width
    text_align: str = 'center',
    font_family: str = 'Arial',
    text_color: Sequence[int] = (0, 0, 0),           # Black
    border_color: Sequence[int] = (255, 255, 255),   # White
) -> bool:
    """
    Add a white border with caption text to an image using percentage-based sizing.
    
    Args:
        image_path (str): Path to the image file
        caption (str): Caption text to add
        border_percent (float): Border height as % of image height (e.g., 5.0 = 5%)
        font_percent (float): Font size as % of image height (e.g., 1.5 = 1.5%)
        padding_percent (float): Text padding as % of image width (e.g., 1.0 = 1%)
        text_align (str): Text alignment ('left', 'center', 'right')
        font_family (str): Font family name
        text_color (tuple): RGB color for text
        border_color (tuple): RGB color for border
    """
    
    # Open the image
    try:
        img = Image.open(image_path)
    except Exception as e:
        print(f"Error opening image: {e}", file=sys.stderr)
        return False
    
    # Convert to RGB if necessary (handles RGBA, grayscale, etc.)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    # Get original dimensions
    orig_width, orig_height = img.size
    
    print(f"Image dimensions: {orig_width} x {orig_height} px")
    
    # Calculate actual pixel values from percentages
    border_height = int(orig_height * (border_percent / 100.0))
    font_size = int(orig_height * (font_percent / 100.0))
    padding = int(orig_width * (padding_percent / 100.0))
    
    # Ensure minimum sizes
    border_height = max(border_height, 40)   # Minimum 40px
    font_size = max(font_size, 12)           # Minimum 12pt
    padding = max(padding, 10)                # Minimum 10px
    
    print(f"Calculated sizes:")
    print(f"  Border height: {border_height} px ({border_percent}% of {orig_height}px)")
    print(f"  Font size: {font_size} pt ({font_percent}% of {orig_height}px)")
    print(f"  Padding: {padding} px ({padding_percent}% of {orig_width}px)")
    
    # Create new image with border
    new_height = orig_height + border_height
    new_img = Image.new('RGB', (orig_width, new_height), border_color)
    
    # Paste original image at top
    new_img.paste(img, (0, 0))
    
    # Add caption text
    draw = ImageDraw.Draw(new_img)
    
    # Try to load font
    try:
        # Try to find system font
        font = ImageFont.truetype(font_family, font_size)
    except:
        try:
            # Try common font paths
            font_paths: Iterable[str] = [
                f"/System/Library/Fonts/{font_family}.ttc",  # macOS
                f"/System/Library/Fonts/{font_family}.ttf",
                f"/System/Library/Fonts/Supplemental/{font_family}.ttf",
                f"C:\\Windows\\Fonts\\{font_family.lower()}.ttf",  # Windows
                f"C:\\Windows\\Fonts\\{font_family}.ttf",
                f"/usr/share/fonts/truetype/liberation/Liberation{font_family}-Regular.ttf",  # Linux
                f"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux fallback
            ]
            
            font = None
            for path in font_paths:
                if os.path.exists(path):
                    font = ImageFont.truetype(path, font_size)
                    print(f"Loaded font from: {path}")
                    break
            
            if font is None:
                # Fallback to default font
                print(f"Warning: Could not load {font_family}, using default font", file=sys.stderr)
                # Use a basic font at calculated size
                try:
                    font = ImageFont.load_default()
                except:
                    font = None
        except Exception as e:
            print(f"Warning: Font loading error: {e}", file=sys.stderr)
            font = ImageFont.load_default()
    
    # Word wrap caption if too long
    caption_lines = wrap_text(caption, font, orig_width - (padding * 2), draw)
    
    print(f"Caption wrapped into {len(caption_lines)} line(s)")
    
    # Calculate text dimensions
    # Get line height from font
    if hasattr(font, 'getbbox'):
        sample_bbox = font.getbbox('Ay')
        line_height = sample_bbox[3] - sample_bbox[1]
    else:
        line_height = font_size
    
    line_spacing = int(line_height * 0.2)  # 20% spacing between lines
    total_text_height = len(caption_lines) * line_height + (len(caption_lines) - 1) * line_spacing
    
    # Calculate vertical position (center text in border)
    text_y_start = orig_height + (border_height - total_text_height) // 2
    
    # Ensure text fits in border
    if total_text_height > border_height * 0.9:  # Text takes more than 90% of border
        print(f"Warning: Caption may not fit well. Consider increasing border height.", file=sys.stderr)
    
    # Draw each line
    for i, line in enumerate(caption_lines):
        # Get text bounding box for this line
        if hasattr(font, 'getbbox'):
            bbox = font.getbbox(line)
            text_width = bbox[2] - bbox[0]
        else:
            # Fallback for older PIL versions
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
        
        # Calculate horizontal position based on alignment
        if text_align == 'center':
            text_x = (orig_width - text_width) // 2
        elif text_align == 'right':
            text_x = orig_width - text_width - padding
        else:  # left
            text_x = padding
        
        # Draw text
        line_y = text_y_start + (i * (line_height + line_spacing))
        draw.text((text_x, line_y), line, fill=text_color, font=font)
        
        print(f"  Line {i+1}: '{line}' at position ({text_x}, {line_y})")
    
    # Save the image (replace original)
    try:
        # Save with high quality
        new_img.save(image_path, quality=95, optimize=True)
        print(f"✓ Successfully added caption border to: {image_path}")
        print(f"  New dimensions: {orig_width} x {new_height} px")
        return True
    except Exception as e:
        print(f"Error saving image: {e}", file=sys.stderr)
        return False


def wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """
    Wrap text to fit within max_width.
    
    Args:
        text (str): Text to wrap
        font: PIL ImageFont
        max_width (int): Maximum width in pixels
        draw: PIL ImageDraw object
    
    Returns:
        list: List of wrapped text lines
    """
    words = text.split()
    lines = []
    current_line = []
    
    for word in words:
        # Try adding this word to current line
        test_line = ' '.join(current_line + [word])
        
        # Get text width
        if hasattr(font, 'getbbox'):
            bbox = font.getbbox(test_line)
            test_width = bbox[2] - bbox[0]
        else:
            bbox = draw.textbbox((0, 0), test_line, font=font)
            test_width = bbox[2] - bbox[0]
        
        if test_width <= max_width:
            current_line.append(word)
        else:
            # Line is too long, start new line
            if current_line:
                lines.append(' '.join(current_line))
            current_line = [word]
    
    # Add last line
    if current_line:
        lines.append(' '.join(current_line))
    
    # If no lines (single word too long), force it
    if not lines and words:
        lines = [text]
    
    return lines


def main() -> int:
    """CLI entry: add a Polaroid-style caption border to one image.

    Invoked per rendered export by the plugin's Polaroid filter as
    ``python -m photokin.lightroom.caption_border``. Parses sizing flags
    (percentage-based so output is resolution-independent) and returns a process
    exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        description='Add Polaroid-style caption border to an image using percentage-based sizing'
    )
    
    parser.add_argument(
        'image_path',
        help='Path to the image file'
    )
    
    parser.add_argument(
        '--caption',
        required=True,
        help='Caption text to add'
    )
    
    parser.add_argument(
        '--border-percent',
        type=float,
        default=5.0,
        help='Border height as percentage of image height (default: 5.0)'
    )
    
    parser.add_argument(
        '--font-percent',
        type=float,
        default=1.5,
        help='Font size as percentage of image height (default: 1.5)'
    )
    
    parser.add_argument(
        '--padding-percent',
        type=float,
        default=1.0,
        help='Text padding as percentage of image width (default: 1.0)'
    )
    
    parser.add_argument(
        '--text-align',
        choices=['left', 'center', 'right'],
        default='center',
        help='Text alignment (default: center)'
    )
    
    parser.add_argument(
        '--font-family',
        default='Arial',
        help='Font family name (default: Arial)'
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Polaroid Caption Border Generator (Percentage-Based)")
    print("=" * 60)
    
    # Validate image path
    if not Path(args.image_path).exists():
        print(f"Error: Image file not found: {args.image_path}", file=sys.stderr)
        return 1
    
    # Process image
    success = add_caption_border(
        image_path=args.image_path,
        caption=args.caption,
        border_percent=args.border_percent,
        font_percent=args.font_percent,
        padding_percent=args.padding_percent,
        text_align=args.text_align,
        font_family=args.font_family
    )
    
    print("=" * 60)
    
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
