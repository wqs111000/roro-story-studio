from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


PAGE_W = 12 * 72
PAGE_H = 9 * 72
STUDIO_ICON_PATH = Path(__file__).resolve().parents[1] / "service" / "assets" / "project-icon.png"


def register_fonts() -> tuple[str, str]:
    candidates = [
        (Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\msyhbd.ttc")),
        (Path(r"C:\Windows\Fonts\simhei.ttf"), Path(r"C:\Windows\Fonts\simhei.ttf")),
        (
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            try:
                pdfmetrics.registerFont(TTFont("RoroCJK", str(regular), subfontIndex=0))
                pdfmetrics.registerFont(TTFont("RoroCJK-Bold", str(bold), subfontIndex=0))
                return "RoroCJK", "RoroCJK-Bold"
            except TTFError:
                continue
    raise FileNotFoundError("No supported Chinese font found on Windows or Linux")


def draw_cover(c: canvas.Canvas, image_path: Path, title: str, regular: str, bold: str) -> None:
    c.drawImage(ImageReader(str(image_path)), 0, 0, PAGE_W, PAGE_H, mask="auto")
    c.saveState()
    c.setFillColor(Color(1, 0.98, 0.92, alpha=0.88))
    c.roundRect(72, PAGE_H - 164, PAGE_W - 144, 102, 22, fill=1, stroke=0)
    c.setFillColor(HexColor("#315A45"))
    c.setFont(bold, 31)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 111, title)
    c.setFont(regular, 13)
    c.setFillColor(HexColor("#59705D"))
    c.drawCentredString(PAGE_W / 2, PAGE_H - 141, "家庭原创绘本 · 适合 4 岁亲子共读")
    c.restoreState()
    c.showPage()


def draw_story_page(
    c: canvas.Canvas,
    page: dict,
    image_path: Path,
    regular: str,
    bold: str,
) -> None:
    c.drawImage(ImageReader(str(image_path)), 0, 0, PAGE_W, PAGE_H, mask="auto")
    body_style = ParagraphStyle(
        "body",
        fontName=regular,
        fontSize=13.4,
        leading=19.2,
        textColor=HexColor("#3D3B35"),
        alignment=TA_LEFT,
    )
    dialogue_style = ParagraphStyle(
        "dialogue",
        fontName=regular,
        fontSize=12.6,
        leading=17.5,
        textColor=HexColor("#3D3B35"),
        alignment=TA_LEFT,
        leftIndent=3,
    )
    note_style = ParagraphStyle(
        "interaction",
        fontName=regular,
        fontSize=10.5,
        leading=14.5,
        textColor=HexColor("#32667A"),
        backColor=HexColor("#E8F3F4"),
        borderPadding=(4, 7, 4, 7),
        borderRadius=6,
    )

    panel_x = 28
    panel_y = 22
    panel_w = PAGE_W - panel_x * 2
    content_x = panel_x + 62
    content_w = panel_w - 84
    blocks: list[tuple[Paragraph, float, float]] = []
    narration = Paragraph(escape(str(page.get("narration", ""))), body_style)
    _, narration_h = narration.wrap(content_w, PAGE_H)
    blocks.append((narration, narration_h, 6))
    for line in page.get("dialogue", []):
        speaker = escape(str(line.get("speaker", "角色")))
        text = escape(str(line.get("text", "")))
        dialogue = Paragraph(
            f'<font name="{bold}" color="#315A45">{speaker}：</font>{text}',
            dialogue_style,
        )
        _, dialogue_h = dialogue.wrap(content_w, PAGE_H)
        blocks.append((dialogue, dialogue_h, 3))
    if page.get("interaction"):
        note = Paragraph(
            f'<font name="{bold}">亲子互动：</font>{escape(str(page["interaction"]))}',
            note_style,
        )
        _, note_h = note.wrap(content_w, PAGE_H)
        blocks.append((note, note_h, 0))

    content_h = sum(height + gap for _, height, gap in blocks)
    panel_h = max(94, content_h + 28)
    if panel_h > 188:
        raise ValueError(f"Page {page.get('page')} text needs {panel_h:.1f}pt; shorten copy or adjust layout")

    c.saveState()
    c.setFillColor(Color(0.12, 0.18, 0.15, alpha=0.16))
    c.roundRect(panel_x + 3, panel_y - 3, panel_w, panel_h, 17, fill=1, stroke=0)
    c.setFillColor(Color(1, 0.99, 0.96, alpha=0.94))
    c.setStrokeColor(Color(0.29, 0.44, 0.36, alpha=0.28))
    c.setLineWidth(0.8)
    c.roundRect(panel_x, panel_y, panel_w, panel_h, 17, fill=1, stroke=1)

    badge_x = panel_x + 30
    badge_y = panel_y + panel_h - 28
    c.setFillColor(HexColor("#4B705D"))
    c.circle(badge_x, badge_y, 17, fill=1, stroke=0)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont(bold, 12)
    c.drawCentredString(badge_x, badge_y - 4.5, str(page["page"]))

    cursor = panel_y + panel_h - 14
    for block, height, gap in blocks:
        cursor -= height
        block.drawOn(c, content_x, cursor)
        cursor -= gap

    c.restoreState()
    c.showPage()


def draw_end_page(c: canvas.Canvas, title: str, regular: str, bold: str) -> None:
    c.setFillColor(HexColor("#F5F0E2"))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(HexColor("#4B705D"))
    c.setFont(bold, 30)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 150, "故事讲完啦")
    title_style = ParagraphStyle(
        "end-title",
        fontName=bold,
        fontSize=19,
        leading=27,
        textColor=HexColor("#4B705D"),
        alignment=TA_CENTER,
    )
    title_para = Paragraph(f"《{escape(title)}》", title_style)
    _, title_h = title_para.wrap(PAGE_W - 220, 70)
    title_para.drawOn(c, 110, PAGE_H - 220 - title_h)
    c.setFillColor(HexColor("#5F5B50"))
    c.setFont(regular, 15)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 285, "可以再翻一翻，看看你最喜欢哪一页。")
    icon_size = 70
    c.drawImage(
        ImageReader(str(STUDIO_ICON_PATH)),
        PAGE_W / 2 - icon_size / 2,
        PAGE_H - 375 - icon_size / 2,
        icon_size,
        icon_size,
        mask="auto",
    )
    c.setFillColor(HexColor("#6D796F"))
    c.setFont(regular, 12)
    c.drawCentredString(PAGE_W / 2, 62, "Roro Story Studio · 家庭试制版 · 2026")
    c.setFont(regular, 10)
    c.drawCentredString(PAGE_W / 2, 42, "roro.iepose.cn")
    c.showPage()


def draw_reader_extra_page(
    c: canvas.Canvas,
    title: str,
    body: str,
    regular: str,
    bold: str,
) -> None:
    """Add designed back matter instead of an empty booklet-padding page."""
    c.setFillColor(HexColor("#F5F0E2"))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(HexColor("#4B705D"))
    c.setFont(bold, 28)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 150, title)
    body_style = ParagraphStyle(
        "extra-body",
        fontName=regular,
        fontSize=15,
        leading=24,
        textColor=HexColor("#5F5B50"),
        alignment=TA_CENTER,
    )
    body_para = Paragraph(escape(body), body_style)
    _, body_h = body_para.wrap(PAGE_W - 220, 120)
    body_para.drawOn(c, 110, PAGE_H - 230 - body_h)
    icon_size = 70
    c.drawImage(
        ImageReader(str(STUDIO_ICON_PATH)),
        PAGE_W / 2 - icon_size / 2,
        PAGE_H - 390 - icon_size / 2,
        icon_size,
        icon_size,
        mask="auto",
    )
    c.setFillColor(HexColor("#6D796F"))
    c.setFont(regular, 11)
    c.drawCentredString(PAGE_W / 2, 62, "Roro Story Studio · 家庭原创绘本")
    c.showPage()


def build_from_assets(
    story_path: Path,
    cover_path: Path,
    page_paths: list[Path],
    output_path: Path,
) -> None:
    regular, bold = register_fonts()
    story = json.loads(story_path.read_text(encoding="utf-8"))
    pages = [item for item in story.get("pages", []) if isinstance(item, dict)]
    required = [cover_path, *page_paths, STUDIO_ICON_PATH]
    missing = [str(path) for path in required if not path.is_file()]
    if len(page_paths) != len(pages):
        raise ValueError(f"Expected {len(pages)} page images, got {len(page_paths)}")
    if missing:
        raise FileNotFoundError(f"Missing illustration files: {missing}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=(PAGE_W, PAGE_H), pageCompression=1)
    c.setTitle(story["title"])
    c.setAuthor("Roro Story Studio")
    c.setSubject("A family review picture book for Youyou")

    draw_cover(c, cover_path, str(story.get("title", "家庭绘本")), regular, bold)
    for page, image_path in zip(pages, page_paths):
        draw_story_page(
            c,
            page,
            image_path,
            regular,
            bold,
        )
    draw_end_page(c, str(story.get("title", "家庭绘本")), regular, bold)
    page_count = 2 + len(pages)
    extra_pages = (4 - page_count % 4) % 4
    extra_content = [
        ("亲子共读小卡", "你最喜欢故事里的哪一页？可以和爸爸妈妈说一说。"),
        ("下次再见，Roro！", "把喜欢的画面记在心里，下一次我们继续讲故事。"),
        ("故事收藏卡", "读完一本故事，给自己一个温暖的拥抱。"),
    ]
    for index in range(extra_pages):
        title, body = extra_content[index]
        draw_reader_extra_page(c, title, body, regular, bold)
    c.save()


def build(story_path: Path, image_dir: Path, output_path: Path) -> None:
    """Backward-compatible CLI builder using the conventional v1 assets."""
    story = json.loads(story_path.read_text(encoding="utf-8"))
    pages = [item for item in story.get("pages", []) if isinstance(item, dict)]
    cover = image_dir / "cover-v1.png"
    page_paths = [image_dir / f"page-{int(page['page']):02d}-v1.png" for page in pages]
    build_from_assets(story_path, cover, page_paths, output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.story.resolve(), args.images.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
