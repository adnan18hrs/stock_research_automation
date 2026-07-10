from datetime import datetime
from pathlib import Path


DATA = [
    ("Mar-2015", 17.17, 9.05, 8.12),
    ("Apr-2015", 18.47, 9.53, 8.94),
    ("May-2015", 12.58, 9.90, 2.68),
    ("Jun-2015", 22.84, 9.86, 12.98),
    ("Sep-2015", 14.59, 10.91, 3.68),
    ("Oct-2015", 7.13, 10.79, -3.66),
    ("Nov-2015", 19.98, 10.75, 9.23),
    ("Dec-2015", 28.73, 10.96, 17.77),
    ("Jan-2016", 23.85, 11.27, 12.58),
    ("Feb-2016", 30.70, 11.70, 19.00),
    ("Mar-2016", 19.28, 12.28, 7.00),
    ("Oct-2016", 15.53, 10.88, 4.65),
    ("Nov-2016", 4.89, 11.13, -6.24),
    ("Jan-2017", 18.95, 11.96, 6.99),
    ("Feb-2017", -1.39, 11.32, -12.71),
    ("Mar-2017", 5.61, 11.11, -5.50),
    ("Apr-2017", 11.77, 10.83, 0.94),
    ("May-2017", 6.39, 10.83, -4.44),
    ("Jun-2017", 16.68, 10.55, 6.13),
    ("Jul-2017", 20.06, 10.65, 9.41),
    ("Aug-2017", 15.21, 10.13, 5.08),
    ("Sep-2017", 18.00, 10.41, 7.59),
    ("Oct-2017", 15.23, 10.66, 4.57),
    ("Nov-2017", 24.75, 10.04, 14.71),
    ("Dec-2017", 16.12, 10.54, 5.58),
    ("Jan-2018", 31.51, 10.24, 21.27),
    ("Feb-2018", 15.05, 9.65, 5.40),
    ("Apr-2018", 18.02, 10.86, 7.16),
    ("May-2018", 18.45, 10.32, 8.13),
    ("Jun-2018", 11.58, 10.47, 1.11),
    ("Jul-2018", 12.21, 10.63, 1.58),
    ("May-2019", 15.25, 10.45, 4.80),
    ("Jan-2020", 41.90, 10.94, 30.96),
    ("Feb-2020", 21.40, 11.78, 9.62),
    ("Mar-2020", 21.63, 12.82, 8.81),
    ("Apr-2020", 35.52, 18.53, 16.99),
    ("Sep-2020", 24.15, 13.42, 10.73),
    ("Nov-2020", 20.03, 13.50, 6.53),
    ("Dec-2020", 45.19, 11.37, 33.82),
    ("Jan-2021", 27.33, 10.21, 17.12),
    ("Feb-2021", 29.01, 10.00, 19.01),
    ("Mar-2021", 31.23, 9.47, 21.76),
    ("Apr-2021", 8.37, 9.48, -1.11),
    ("May-2021", 21.95, 9.97, 11.98),
    ("Jun-2021", 37.12, 8.81, 28.31),
    ("Jul-2021", 21.86, 8.81, 13.05),
    ("Aug-2021", 16.94, 8.69, 8.25),
    ("Sep-2021", 29.66, 7.23, 22.43),
    ("Oct-2021", 18.64, 6.77, 11.87),
    ("Nov-2021", 19.19, 6.39, 12.80),
    ("Dec-2021", 21.88, 7.52, 14.36),
    ("Jan-2022", 42.04, 7.04, 35.00),
    ("Feb-2022", 21.35, 7.24, 14.11),
    ("Mar-2022", 34.06, 8.78, 25.28),
    ("Apr-2022", 43.71, 7.39, 36.32),
    ("May-2022", 12.40, 8.44, 3.96),
    ("Jul-2022", 6.57, 10.99, -4.42),
    ("Aug-2022", 12.06, 8.56, 3.50),
    ("Oct-2022", 18.07, 9.73, 8.34),
    ("Nov-2022", 15.46, 7.84, 7.62),
    ("Dec-2022", 9.75, 6.95, 2.80),
    ("Feb-2023", 33.44, 9.38, 24.06),
    ("Mar-2023", 39.00, 9.91, 29.09),
    ("Apr-2023", 9.44, 10.28, -0.84),
    ("May-2023", 21.20, 9.11, 12.09),
    ("Jun-2023", 33.24, 8.71, 24.53),
    ("Jul-2023", 19.59, 7.38, 12.21),
    ("Aug-2023", 12.55, 6.83, 5.72),
    ("Sep-2023", 26.59, 7.62, 18.97),
    ("Oct-2023", 19.64, 7.66, 11.98),
    ("Nov-2023", 13.03, 9.05, 3.98),
    ("Dec-2023", 47.34, 6.64, 40.70),
    ("Jan-2024", 8.82, 3.94, 4.88),
    ("Feb-2024", 17.62, 4.16, 13.46),
    ("Mar-2024", 5.59, 3.03, 2.56),
    ("Apr-2024", 11.35, 2.89, 8.46),
    ("May-2024", 33.99, 2.61, 31.38),
    ("Jun-2024", 14.71, 1.42, 13.29),
    ("Jul-2024", -24.51, -0.37, -24.14),
    ("Aug-2024", 4.13, -2.19, 6.32),
    ("Sep-2024", -11.51, -2.85, -8.66),
    ("Oct-2024", -16.99, -4.08, -12.91),
    ("Nov-2024", -14.29, -0.84, -13.45),
    ("Jan-2025", 0.55, 0.61, -0.06),
    ("Sep-2025", -2.14, -3.15, 1.01),
    ("Oct-2025", 17.02, -4.55, 21.57),
    ("Dec-2025", 60.94, -13.64, 74.58),
    ("Jan-2026", 94.79, -15.51, 110.30),
    ("Feb-2026", 17.57, -10.07, 27.64),
]


def scale(value, src_min, src_max, dst_min, dst_max):
    return dst_min + (value - src_min) * (dst_max - dst_min) / (src_max - src_min)


def svg_text(x, y, text, size=14, fill="#27313a", anchor="start", weight="400"):
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="Inter, Arial, sans-serif" '
        f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">'
        f"{text}</text>"
    )


def main():
    dates = [datetime.strptime(row[0], "%b-%Y") for row in DATA]
    portfolio = [row[1] for row in DATA]
    nifty = [row[2] for row in DATA]
    alpha = [row[3] for row in DATA]

    width, height = 1600, 920
    left, right = 120, 60
    top_y1, top_y2 = 150, 560
    bottom_y1, bottom_y2 = 655, 835
    x_min = dates[0].toordinal()
    x_max = dates[-1].toordinal()
    x1, x2 = left, width - right
    top_min, top_max = -30, 100
    alpha_min, alpha_max = -30, 115

    def x_for(date):
        return scale(date.toordinal(), x_min, x_max, x1, x2)

    def y_top(value):
        return scale(value, top_min, top_max, top_y2, top_y1)

    def y_alpha(value):
        return scale(value, alpha_min, alpha_max, bottom_y2, bottom_y1)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f7f8fa"/>',
        '<rect x="90" y="95" width="1460" height="790" rx="10" fill="#ffffff" stroke="#dde1e6"/>',
        svg_text(width / 2, 55, "Portfolio CAGR vs Nifty CAGR", 30, "#17212b", "middle", "700"),
        svg_text(
            width / 2,
            85,
            "Only months with at least 3 passing stocks included. Monthly investment assumption: Rs 100.",
            15,
            "#5c6770",
            "middle",
        ),
    ]

    # Legend
    parts.extend(
        [
            '<line x1="120" y1="118" x2="160" y2="118" stroke="#0b7285" stroke-width="4"/>',
            '<circle cx="140" cy="118" r="5" fill="#0b7285"/>',
            svg_text(170, 123, "Portfolio CAGR", 15, "#27313a"),
            '<line x1="310" y1="118" x2="350" y2="118" stroke="#495057" stroke-width="4"/>',
            '<circle cx="330" cy="118" r="5" fill="#495057"/>',
            svg_text(360, 123, "Nifty CAGR", 15, "#27313a"),
            '<rect x="480" y="108" width="22" height="16" fill="#2f9e44" opacity="0.82"/>',
            svg_text(512, 123, "Positive Alpha", 15, "#27313a"),
            '<rect x="665" y="108" width="22" height="16" fill="#c92a2a" opacity="0.82"/>',
            svg_text(697, 123, "Negative Alpha", 15, "#27313a"),
        ]
    )

    # Grid and axes.
    for tick in range(-20, 101, 20):
        y = y_top(tick)
        color = "#adb5bd" if tick == 0 else "#e7ebef"
        width_line = 1.4 if tick == 0 else 1
        parts.append(f'<line x1="{x1}" y1="{y:.1f}" x2="{x2}" y2="{y:.1f}" stroke="{color}" stroke-width="{width_line}"/>')
        parts.append(svg_text(100, y + 5, f"{tick}%", 13, "#69737d", "end"))
    parts.append(svg_text(38, (top_y1 + top_y2) / 2, "CAGR (%)", 15, "#27313a", "middle", "600").replace("<text ", '<text transform="rotate(-90 38 355)" '))

    for tick in range(-20, 116, 20):
        y = y_alpha(tick)
        color = "#343a40" if tick == 0 else "#e7ebef"
        width_line = 1.3 if tick == 0 else 1
        parts.append(f'<line x1="{x1}" y1="{y:.1f}" x2="{x2}" y2="{y:.1f}" stroke="{color}" stroke-width="{width_line}"/>')
        parts.append(svg_text(100, y + 5, f"{tick}%", 13, "#69737d", "end"))
    parts.append(svg_text(38, (bottom_y1 + bottom_y2) / 2, "Alpha (%)", 15, "#27313a", "middle", "600").replace("<text ", '<text transform="rotate(-90 38 745)" '))

    # Year ticks.
    for year in range(2015, 2027):
        date = datetime(year, 1, 1)
        if dates[0] <= date <= dates[-1]:
            x = x_for(date)
            parts.append(f'<line x1="{x:.1f}" y1="{top_y1}" x2="{x:.1f}" y2="{bottom_y2}" stroke="#f0f2f4" stroke-width="1"/>')
            parts.append(svg_text(x, 870, str(year), 14, "#5c6770", "middle"))
    parts.append(svg_text((x1 + x2) / 2, 905, "Portfolio Date", 15, "#27313a", "middle", "600"))

    # Alpha bars.
    zero_y = y_alpha(0)
    bar_width = 7
    for date, value in zip(dates, alpha):
        x = x_for(date) - bar_width / 2
        y = y_alpha(max(value, 0))
        h = abs(y_alpha(value) - zero_y)
        color = "#2f9e44" if value >= 0 else "#c92a2a"
        parts.append(f'<rect x="{x:.1f}" y="{min(y, zero_y):.1f}" width="{bar_width}" height="{h:.1f}" fill="{color}" opacity="0.82"/>')

    def line_path(values, y_func):
        commands = []
        for index, (date, value) in enumerate(zip(dates, values)):
            prefix = "M" if index == 0 else "L"
            commands.append(f"{prefix}{x_for(date):.1f},{y_func(value):.1f}")
        return " ".join(commands)

    parts.append(f'<path d="{line_path(portfolio, y_top)}" fill="none" stroke="#0b7285" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>')
    parts.append(f'<path d="{line_path(nifty, y_top)}" fill="none" stroke="#495057" stroke-width="3.4" stroke-linejoin="round" stroke-linecap="round"/>')

    for date, value in zip(dates, portfolio):
        parts.append(f'<circle cx="{x_for(date):.1f}" cy="{y_top(value):.1f}" r="3.8" fill="#0b7285"/>')
    for date, value in zip(dates, nifty):
        parts.append(f'<circle cx="{x_for(date):.1f}" cy="{y_top(value):.1f}" r="3.2" fill="#495057"/>')

    # Call out the highest and lowest portfolio CAGR points.
    max_index = portfolio.index(max(portfolio))
    min_index = portfolio.index(min(portfolio))
    for label, index, dy in [
        ("Highest Portfolio CAGR", max_index, -16),
        ("Lowest Portfolio CAGR", min_index, 24),
    ]:
        x = x_for(dates[index])
        y = y_top(portfolio[index])
        parts.append(svg_text(x, y + dy, f"{label}: {DATA[index][0]} ({portfolio[index]:.2f}%)", 13, "#17212b", "middle", "600"))

    parts.append("</svg>")

    output = Path(__file__).with_name("portfolio_cagr_vs_nifty_chart.svg")
    output.write_text("\n".join(parts), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
