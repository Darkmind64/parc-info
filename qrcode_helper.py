"""
qrcode_helper.py — QR Code label generation for AVERY J8159 sheets.

Provides utilities to:
1. Generate QR codes from asset data (plain text format for phone barcode scanners)
2. Create label images (63.5mm × 33.9mm) with QR, logo, and text
3. Generate PDF sheets (A4, 3×8 grid) for printing on AVERY J8159 labels
"""

import json
import qrcode
import io
import os
import tempfile
import logging
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib import colors

logger = logging.getLogger('parcinfo')


def hex_to_rgb(hex_color: str) -> tuple:
    """Convert hex color (#RRGGBB) to RGB tuple (R, G, B)."""
    if not hex_color:
        return (255, 255, 255)  # default white

    hex_color = hex_color.lstrip('#')
    if len(hex_color) != 6:
        return (255, 255, 255)  # default white if invalid

    try:
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    except:
        return (255, 255, 255)  # default white if conversion fails


def generate_qr(data: dict, size: int = 5, qr_color: str = "black", qr_bg_color: str = "white", border: int = 1) -> Image.Image:
    """Generate QR code from asset data dictionary.

    Args:
        data: Dictionary with asset information (nom, adresse_ip, credentials, etc)
        size: Box size in pixels (default 5 pixels per box for v2-v6 codes)
        qr_color: QR code color (hex or name, default "black")
        qr_bg_color: QR background color (hex or name, default "white")
        border: Border size in boxes (default 1)

    Returns:
        PIL Image object containing QR code
    """
    # Format data as readable plain text instead of JSON
    # This allows phone barcode scanners to display the content as text
    field_labels = {
        'nom': 'Nom',
        'adresse_ip': 'IP',
        'adresse_mac': 'MAC',
        'nom_dns': 'DNS',
        'ports_ouverts': 'Ports',
        'user_login': 'User',
        'user_password': 'Password',
        'admin_login': 'Admin',
        'admin_password': 'Admin Pwd',
        'type': 'Type',
        'marque': 'Marque',
        'modele': 'Modèle',
        'numero_serie': 'S/N',
    }

    text_lines = []
    for key, value in data.items():
        # Skip internal fields and empty values
        if key in ['type', 'id'] or not value:
            continue

        # Get friendly label or use key as-is
        label = field_labels.get(key, key.replace('_', ' ').title())
        text_lines.append(f"{label}: {value}")

    text_str = '\n'.join(text_lines)

    qr = qrcode.QRCode(
        version=None,  # Auto-detect version based on data size
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=size,
        border=border,
    )
    qr.add_data(text_str)
    qr.make(fit=True)
    return qr.make_image(fill_color=qr_color, back_color=qr_bg_color)


def create_label_image(
    asset_data: dict,
    logo_path: str | None = None,
    custom_text: str = "",
    template: dict | None = None
) -> Image.Image:
    """Create a single label image (63.5mm × 33.9mm at 300 DPI) with customizable layout.

    Layout based on template parameters:
    - QR size: small (30%), medium (45%), large (60%)
    - QR position: left, center, right
    - QR color, QR background color, QR border
    - Logo: size (0-30mm), position (left/top/right/bottom/center), opacity, border
    - Text: color, font, size, custom header/footer
    - Asset info text (nom, IP, user) and custom footer

    Args:
        asset_data: Dictionary with asset info (will be encoded in QR)
        logo_path: Path to logo image file (PNG/JPG), optional
        custom_text: Custom footer text to display on label (max 200 chars)
        template: Optional dict with layout parameters:
            - qrSize: 'small'|'medium'|'large' or float 0-1
            - qrPosition: 'left'|'center'|'right'
            - qrColor: hex color (default '#000000')
            - qrBgColor: hex color (default '#ffffff')
            - qrBorder: int 0-4 (default 1)
            - logoSize: float in mm (0-30, default 8)
            - logoPosition: 'left'|'top'|'right'|'bottom'|'center' (default auto)
            - logoOpacity: 0-100 (default 100)
            - logoBorder: bool (default False)
            - logoBorderColor: hex color
            - logoBorderWidth: int in pixels
            - textSize: 'small'|'medium'|'large'
            - textColor: hex color (default '#000000')
            - textFont: 'arial'|'courier'|'times' (default 'arial')
            - customHeader: text before asset name
            - customFooter: text after asset info

    Returns:
        PIL Image object with label (63.5mm × 33.9mm)
    """
    # Default template
    if template is None:
        template = {}

    # Convert dimensions: 63.5mm W × 33.9mm H at 300 DPI = ~750×400 pixels
    label_width_mm = 63.5
    label_height_mm = 33.9
    dpi = 300
    label_width = int((label_width_mm / 25.4) * dpi)
    label_height = int((label_height_mm / 25.4) * dpi)

    # Create blank label with optional background color
    bg_color_hex = template.get('bgColor', '#ffffff')
    bg_color_rgb = hex_to_rgb(bg_color_hex)
    label = Image.new('RGB', (label_width, label_height), color=bg_color_rgb)
    draw = ImageDraw.Draw(label)

    # Margins and sizing (in pixels at 300 DPI)
    margin = int(10)  # ~3mm margin

    # QR Code Parameters
    qr_size_param = template.get('qrSize', 'medium')
    if isinstance(qr_size_param, str):
        qr_size_percent = {'small': 0.30, 'medium': 0.45, 'large': 0.60}.get(qr_size_param, 0.45)
    else:
        qr_size_percent = min(1.0, max(0.1, float(qr_size_param)))
    qr_size = int(label_height * qr_size_percent)

    qr_color = template.get('qrColor', '#000000')
    qr_bg_color = template.get('qrBgColor', '#ffffff')
    qr_border = template.get('qrBorder', 1)

    # Logo Parameters (now in mm via slider, 0-30mm)
    logo_size_mm = template.get('logoSize', 8)  # Default 8mm
    logo_size = int((logo_size_mm / 25.4) * dpi) if logo_size_mm > 0 else 0
    logo_position = template.get('logoPosition', 'auto')  # 'left'|'right'|'top'|'bottom'|'center'|'auto'
    logo_opacity = template.get('logoOpacity', 100)  # 0-100
    logo_border = template.get('logoBorder', False)
    logo_border_color = template.get('logoBorderColor', '#000000')
    logo_border_width = template.get('logoBorderWidth', 1)

    # Text Parameters - Support both new (numeric) and legacy (small/medium/large) formats
    font_map = {
        'arial': 'arial.ttf',
        'courier': 'cour.ttf',
        'times': 'times.ttf'
    }

    # Helper function to convert textSize format to points
    def get_font_size_pt(size_param, default=10):
        if isinstance(size_param, int):
            return size_param
        elif isinstance(size_param, str):
            if size_param.isdigit():
                return int(size_param)
            legacy_map = {'small': 8, 'medium': 10, 'large': 12}
            return legacy_map.get(size_param.lower(), default)
        return default

    # Helper function to load font
    def load_font(font_name, size_pt):
        try:
            ttf_name = font_map.get(font_name, 'arial.ttf')
            return ImageFont.truetype(ttf_name, size_pt)
        except:
            return ImageFont.load_default()

    # Header text settings
    custom_header = template.get('customHeader', '')
    header_size_pt = get_font_size_pt(template.get('headerSize', 12))
    header_color = hex_to_rgb(template.get('headerColor', '#000000'))
    header_font_name = template.get('headerFont', 'arial')
    header_align = template.get('headerAlign', 'left')  # left, center, right
    header_font = load_font(header_font_name, header_size_pt)

    # Asset fields text settings
    asset_size_pt = get_font_size_pt(template.get('assetSize', 10))
    asset_color = hex_to_rgb(template.get('assetColor', '#000000'))
    asset_font_name = template.get('assetFont', 'arial')
    asset_align = template.get('assetAlign', 'left')
    asset_font = load_font(asset_font_name, asset_size_pt)

    # Footer text settings
    custom_footer = template.get('customFooter', '')
    footer_size_pt = get_font_size_pt(template.get('footerSize', 10))
    footer_color = hex_to_rgb(template.get('footerColor', '#000000'))
    footer_font_name = template.get('footerFont', 'arial')
    footer_align = template.get('footerAlign', 'left')
    footer_font = load_font(footer_font_name, footer_size_pt)

    # For compatibility with existing code that might reference font_size
    font_size = asset_size_pt
    line_height = int(asset_size_pt * 1.3)

    # Position QR code based on template
    qr_position = template.get('qrPosition', 'center')
    if qr_position == 'left':
        qr_x = margin
    elif qr_position == 'right':
        qr_x = label_width - qr_size - margin
    else:  # center
        qr_x = label_width // 2 - qr_size // 2

    qr_y = label_height // 2 - qr_size // 2

    # Generate and paste QR code with custom colors/border
    qr_img = generate_qr(asset_data, size=4, qr_color=qr_color, qr_bg_color=qr_bg_color, border=qr_border)
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
    label.paste(qr_img, (qr_x, qr_y))

    # Add logo if provided
    if logo_path and logo_size > 0:
        try:
            logo = Image.open(logo_path)
            logo = logo.convert('RGBA')

            # Apply opacity
            if logo_opacity < 100:
                alpha = logo.split()[3]
                alpha = alpha.point(lambda p: int(p * (logo_opacity / 100)))
                logo.putalpha(alpha)

            logo.thumbnail((logo_size, logo_size), Image.Resampling.LANCZOS)

            # Position logo
            if logo_position == 'auto':
                logo_pos = 'left' if qr_position == 'right' else 'right'
            else:
                logo_pos = logo_position

            if logo_pos == 'left':
                logo_x = margin
                logo_y = label_height // 2 - logo_size // 2
            elif logo_pos == 'right':
                logo_x = label_width - logo_size - margin
                logo_y = label_height // 2 - logo_size // 2
            elif logo_pos == 'top':
                logo_x = label_width // 2 - logo_size // 2
                logo_y = margin
            elif logo_pos == 'bottom':
                logo_x = label_width // 2 - logo_size // 2
                logo_y = label_height - logo_size - margin
            else:  # center
                logo_x = label_width // 2 - logo_size // 2
                logo_y = label_height // 2 - logo_size // 2

            # Add border if requested
            if logo_border:
                border_color_rgb = hex_to_rgb(logo_border_color)
                border_color_rgba = border_color_rgb + (255,)  # Add alpha channel
                border_img = Image.new('RGBA', (logo_size + 2*logo_border_width, logo_size + 2*logo_border_width), color=border_color_rgba)
                border_img.paste(logo, (logo_border_width, logo_border_width), logo)
                logo = border_img
                logo_x -= logo_border_width
                logo_y -= logo_border_width

            label.paste(logo, (logo_x, logo_y), logo)
        except Exception:
            pass  # Skip logo if error loading

    # Helper function to apply text alignment
    def draw_aligned_text(draw_obj, text, x, y, fill_color, font_obj, alignment='left', max_width=None):
        """Draw text with alignment support"""
        text_bbox = draw_obj.textbbox((0, 0), text, font=font_obj)
        text_width = text_bbox[2] - text_bbox[0]

        if alignment == 'center' and max_width:
            x = max_width // 2 - text_width // 2
        elif alignment == 'right' and max_width:
            x = max_width - text_width - margin

        draw_obj.text((x, y), text, fill=fill_color, font=font_obj)

    # Add text based on template
    if qr_position == 'left':
        text_x = qr_x + qr_size + margin
    elif qr_position == 'right':
        text_x = margin
    else:  # center
        text_x = qr_x + qr_size + margin if qr_x + qr_size + margin < label_width // 2 else margin

    text_y = margin

    # Custom header
    if custom_header:
        header_text = custom_header[:30]
        draw_aligned_text(draw, header_text, text_x, text_y, header_color, header_font, header_align, label_width)
        text_y += int(header_size_pt * 1.3)

    # Display all selected fields from asset_data (excluding 'type' and 'id')
    field_labels = {
        'nom': 'Nom',
        'nom_machine': 'Machine',
        'adresse_ip': 'IP',
        'adresse_mac': 'MAC',
        'nom_dns': 'DNS',
        'user_login': 'User',
        'user_password': 'Pwd',
        'admin_login': 'Admin',
        'admin_password': 'Admin Pwd',
        'ports_ouverts': 'Ports',
        'marque': 'Marque',
        'modele': 'Modèle',
        'numero_serie': 'S/N',
        'categorie': 'Catégorie',
        'localisation': 'Localisation',
    }

    # Display asset fields
    asset_line_height = int(asset_size_pt * 1.3)
    for field in sorted(asset_data.keys()):
        # Skip system fields
        if field in ['type', 'id']:
            continue

        value = asset_data.get(field)
        if not value or not str(value).strip():
            continue

        try:
            # Get label for field, or use field name if not in map
            field_label = field_labels.get(field, field.replace('_', ' ').title())

            # Format the value (truncate if too long)
            value_str = str(value)[:35]

            # Create display text
            display_text = f"{field_label}: {value_str}"

            # Check if text fits vertically (leave space for footer)
            # Reserve margin + footer_size_pt for footer
            if text_y + asset_line_height < label_height - margin - footer_size_pt - margin:
                draw_aligned_text(draw, display_text, text_x, text_y, asset_color, asset_font, asset_align, label_width)
                text_y += asset_line_height
        except:
            # Skip field if error rendering it
            pass

    # Custom footer at bottom
    footer_text = custom_footer or custom_text
    logger.info(f"Footer rendering: custom_footer='{custom_footer}', custom_text='{custom_text}', footer_text='{footer_text}'")

    if footer_text:
        footer_text = str(footer_text)[:60]  # Limit custom text length
        logger.info(f"Footer to draw: '{footer_text}' (len={len(footer_text)})")
        try:
            # CRITICAL FIX: Create a fresh ImageDraw context to ensure footer is drawn
            draw_fresh = ImageDraw.Draw(label)

            # Calculate footer position with percentage-based formula
            footer_y = int(label_height * 0.78)
            logger.info(f"Footer position: y={footer_y}, footer_size_pt={footer_size_pt}")
            logger.info(f"Footer color: {footer_color}, font_size: {footer_size_pt}")

            # Draw footer text with alignment
            draw_aligned_text(draw_fresh, footer_text, text_x, int(footer_y), footer_color, footer_font, footer_align, label_width)
            logger.info(f"Footer drawn successfully at y={footer_y} with alignment={footer_align}")
        except Exception as e:
            logger.warning(f"Error drawing footer: {e}")
            import traceback
            logger.warning(f"Traceback: {traceback.format_exc()}")
    else:
        logger.info(f"No footer text to display (custom_footer='{custom_footer}', custom_text='{custom_text}')")

    return label


def create_pdf_sheet(
    label_image: Image.Image,
    positions: dict[int, int],
    left_margin_mm: float = 7.0,
    top_margin_mm: float = 12.5,
    col_spacing_mm: float = 2.5,
    row_spacing_mm: float = 0.0
) -> bytes:
    """Create AVERY J8159 PDF sheet (A4, 3×8 grid) with labels at specified positions.

    AVERY J8159 specifications:
    - Sheet: A4 (210mm × 297mm)
    - Labels: 3 columns × 8 rows = 24 labels per sheet
    - Label size: 63.5mm W × 33.9mm H
    - Top margin: 12.5mm, Left margin: 7.0mm (per modèle Word fourni)
    - Column spacing: 2.5mm (per modèle Word fourni)
    - Row spacing: 0mm (labels are contiguous vertically)

    Args:
        label_image: PIL Image of the label (will be used for all positions)
        positions: Dict mapping label position (1-24) to copy count
                  Example: {1: 1, 5: 2, 24: 1} means label at pos 1, 5 (×2), 24
        left_margin_mm: Left margin in mm (default 7.0)
        top_margin_mm: Top margin in mm (default 12.5)
        col_spacing_mm: Horizontal spacing between labels in same row (default 2.5)
        row_spacing_mm: Vertical spacing between labels in same column (default 0.0)

    Returns:
        PDF bytes ready for download/printing
    """
    pdf_buffer = io.BytesIO()

    # Create canvas with A4 size
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_width, page_height = A4

    # AVERY J8159 specifications (in mm)
    label_width_mm = 63.5
    label_height_mm = 33.9
    cols = 3
    rows = 8

    # Convert to points (1 point = 1/72 inch ≈ 0.353 mm)
    mm_to_pt = 72 / 25.4
    label_width = label_width_mm * mm_to_pt
    label_height = label_height_mm * mm_to_pt
    left_margin = left_margin_mm * mm_to_pt
    top_margin = top_margin_mm * mm_to_pt
    col_spacing = col_spacing_mm * mm_to_pt
    row_spacing = row_spacing_mm * mm_to_pt

    # Create temporary file for label image (portable across Windows/Linux/Mac)
    fd, temp_img_path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        label_image.save(temp_img_path)

        # Calculate positions and place labels
        for pos, count in positions.items():
            if pos < 1 or pos > 24:
                continue

            # Calculate row and column from position (1-based)
            pos_idx = pos - 1
            col = pos_idx % cols
            row = pos_idx // cols

            # Calculate X, Y coordinates with spacing
            # X: left margin + (col × (label_width + col_spacing))
            x = left_margin + (col * (label_width + col_spacing))
            # Y: page_height - top_margin - ((row + 1) × (label_height + row_spacing))
            y = page_height - top_margin - ((row + 1) * (label_height + row_spacing))

            # Draw label 'count' times at this position
            for _ in range(count):
                c.drawImage(temp_img_path, x, y, width=label_width, height=label_height)
    finally:
        # Clean up temporary file
        try:
            os.remove(temp_img_path)
        except:
            pass

    # Save PDF
    c.save()
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()
