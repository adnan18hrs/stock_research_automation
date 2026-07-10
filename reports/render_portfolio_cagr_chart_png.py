from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from create_portfolio_cagr_chart import DATA, scale


WIDTH, HEIGHT = 1800, 1050
SCALE = 2


def load_font(size, bold=False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Helvetica.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size * SCALE)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_text(draw, xy, text, font, fill, anchor="la"):
    x, y = xy
    draw.text((x * SCALE, y * SCALE), text, font=font, fill=fill, anchor=anchor)


def draw_rotated_label(image, text, center, font, fill):
    bbox = font.getbbox(text)
    label = Image.new("RGBA", (bbox[2] - bbox[0] + 24 * SCALE, bbox[3] - bbox[1] + 24 * SCALE), (0, 0, 0, 0))
    label_draw = ImageDraw.Draw(label)
    label_draw.text((12 * SCALE, 12 * SCALE), text, font=font, fill=fill)
    rotated = label.rotate(90, expand=True)
    x = int(center[0] * SCALE - rotated.width / 2)
    y = int(center[1] * SCALE - rotated.height / 2)
    image.alpha_composite(rotated, (x, y))


def main():
    dates = [datetime.strptime(row[0], "%b-%Y") for row in DATA]
    portfolio = [row[1] for row in DATA]
    nifty = [row[2] for row in DATA]
    alpha = [row[3] for row in DATA]

    image = Image.new("RGBA", (WIDTH * SCALE, HEIGHT * SCALE), "#f7f8fa")
    draw = ImageDraw.Draw(image)

    left, right = 130, 70
    top_y1, top_y2 = 170, 615
    bottom_y1, bottom_y2 = 735, 940
    x1, x2 = left, WIDTH - right
    top_min, top_max = -30, 100
    alpha_min, alpha_max = -30, 115
    x_min = dates[0].toordinal()
    x_max = dates[-1].toordinal()

    def sx(date):
        return scale(date.toordinal(), x_min, x_max, x1, x2)

    def sy_top(value):
        return scale(value, top_min, top_max, top_y2, top_y1)

    def sy_alpha(value):
        return scale(value, alpha_min, alpha_max, bottom_y2, bottom_y1)

    title_font = load_font(36, True)
    subtitle_font = load_font(17)
    label_font = load_font(16, True)
    tick_font = load_font(15)
    legend_font = load_font(17)
    note_font = load_font(14, True)

    # Card
    draw.rounded_rectangle(
        [70 * SCALE, 95 * SCALE, (WIDTH - 45) * SCALE, (HEIGHT - 45) * SCALE],
        radius=12 * SCALE,
        fill="#ffffff",
        outline="#dde1e6",
        width=2 * SCALE,
    )

    draw_text(draw, (WIDTH / 2, 48), "Portfolio CAGR vs Nifty CAGR", title_font, "#17212b", "ma")
    draw_text(
        draw,
        (WIDTH / 2, 91),
        "Only months with at least 3 passing stocks included. Monthly investment assumption: Rs 100.",
        subtitle_font,
        "#5c6770",
        "ma",
    )

    # Legend
    y_legend = 130
    draw.line([(140 * SCALE, y_legend * SCALE), (190 * SCALE, y_legend * SCALE)], fill="#0b7285", width=5 * SCALE)
    draw.ellipse([(160 * SCALE, (y_legend - 8) * SCALE), (176 * SCALE, (y_legend + 8) * SCALE)], fill="#0b7285")
    draw_text(draw, (205, y_legend + 6), "Portfolio CAGR", legend_font, "#27313a", "la")
    draw.line([(390 * SCALE, y_legend * SCALE), (440 * SCALE, y_legend * SCALE)], fill="#495057", width=5 * SCALE)
    draw.ellipse([(410 * SCALE, (y_legend - 8) * SCALE), (426 * SCALE, (y_legend + 8) * SCALE)], fill="#495057")
    draw_text(draw, (455, y_legend + 6), "Nifty CAGR", legend_font, "#27313a", "la")
    draw.rectangle([620 * SCALE, (y_legend - 13) * SCALE, 650 * SCALE, (y_legend + 13) * SCALE], fill="#2f9e44")
    draw_text(draw, (668, y_legend + 6), "Positive Alpha", legend_font, "#27313a", "la")
    draw.rectangle([845 * SCALE, (y_legend - 13) * SCALE, 875 * SCALE, (y_legend + 13) * SCALE], fill="#c92a2a")
    draw_text(draw, (893, y_legend + 6), "Negative Alpha", legend_font, "#27313a", "la")

    # Horizontal grid.
    for tick in range(-20, 101, 20):
        y = sy_top(tick)
        color = "#adb5bd" if tick == 0 else "#e7ebef"
        width = 2 if tick == 0 else 1
        draw.line([(x1 * SCALE, y * SCALE), (x2 * SCALE, y * SCALE)], fill=color, width=width * SCALE)
        draw_text(draw, (112, y + 5), f"{tick}%", tick_font, "#69737d", "ra")
    draw_rotated_label(image, "CAGR (%)", (45, (top_y1 + top_y2) / 2), label_font, "#27313a")

    for tick in range(-20, 116, 20):
        y = sy_alpha(tick)
        color = "#343a40" if tick == 0 else "#e7ebef"
        width = 2 if tick == 0 else 1
        draw.line([(x1 * SCALE, y * SCALE), (x2 * SCALE, y * SCALE)], fill=color, width=width * SCALE)
        draw_text(draw, (112, y + 5), f"{tick}%", tick_font, "#69737d", "ra")
    draw_rotated_label(image, "Alpha (%)", (45, (bottom_y1 + bottom_y2) / 2), label_font, "#27313a")

    # Year ticks and vertical grid.
    for year in range(2015, 2027):
        date = datetime(year, 1, 1)
        if dates[0] <= date <= dates[-1]:
            x = sx(date)
            draw.line([(x * SCALE, top_y1 * SCALE), (x * SCALE, bottom_y2 * SCALE)], fill="#eef1f4", width=SCALE)
            draw_text(draw, (x, 982), str(year), tick_font, "#5c6770", "ma")
    draw_text(draw, ((x1 + x2) / 2, 1020), "Portfolio Date", label_font, "#27313a", "ma")

    # Alpha bars.
    zero_y = sy_alpha(0)
    bar_width = 8
    for date, value in zip(dates, alpha):
        x = sx(date)
        y = sy_alpha(value)
        color = "#2f9e44" if value >= 0 else "#c92a2a"
        draw.rectangle(
            [
                (x - bar_width / 2) * SCALE,
                min(y, zero_y) * SCALE,
                (x + bar_width / 2) * SCALE,
                max(y, zero_y) * SCALE,
            ],
            fill=color,
        )

    # Lines and markers.
    portfolio_points = [(sx(date) * SCALE, sy_top(value) * SCALE) for date, value in zip(dates, portfolio)]
    nifty_points = [(sx(date) * SCALE, sy_top(value) * SCALE) for date, value in zip(dates, nifty)]
    draw.line(portfolio_points, fill="#0b7285", width=5 * SCALE, joint="curve")
    draw.line(nifty_points, fill="#495057", width=4 * SCALE, joint="curve")

    for x, y in portfolio_points:
        draw.ellipse([x - 5 * SCALE, y - 5 * SCALE, x + 5 * SCALE, y + 5 * SCALE], fill="#0b7285")
    for x, y in nifty_points:
        draw.ellipse([x - 4 * SCALE, y - 4 * SCALE, x + 4 * SCALE, y + 4 * SCALE], fill="#495057")

    max_index = portfolio.index(max(portfolio))
    min_index = portfolio.index(min(portfolio))
    max_label = f"Highest: {DATA[max_index][0]} ({portfolio[max_index]:.2f}%)"
    min_label = f"Lowest: {DATA[min_index][0]} ({portfolio[min_index]:.2f}%)"
    draw_text(draw, (x2, sy_top(portfolio[max_index]) - 18), max_label, note_font, "#17212b", "ra")
    draw_text(draw, (sx(dates[min_index]), sy_top(portfolio[min_index]) + 26), min_label, note_font, "#17212b", "ma")

    output = Path(__file__).with_name("portfolio_cagr_vs_nifty_chart.png")
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).convert("RGB")
    image.save(output, "PNG", optimize=True)
    print(output)


if __name__ == "__main__":
    main()
