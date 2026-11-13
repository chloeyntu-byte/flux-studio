"""Render the measured optimization summary for the last video frames."""

import math

from emx_search import evaluate_response


BACKGROUND = '#111e2b'
PANEL = '#0c1723'
INK = '#e0ebf3'
MUTED = '#9ab0c1'
GRID = '#2b4050'
TRACE = '#568b99'
BEST = '#d3f0ed'
FEASIBLE = '#79d1d5'
PARETO = '#f2ba78'
OUTSIDE = '#ed93a8'


def _font(image_font, size, bold=False):
    for family in ('DejaVuSans', 'LiberationSans'):
        try:
            return image_font.truetype(family + ('-Bold' if bold else '') + '.ttf', size)
        except OSError:
            pass
    return image_font.load_default()


def _measurement(row, goal):
    if row.get('status') != 'completed' or row.get('data_error'):
        return None
    # One missing objective or constraint should not hide a valid measured
    # value for another objective. Use the same frequency match as optimization.
    try:
        return evaluate_response(row.get('metrics') or [], [goal], [])['objective_values'][0]
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _goal_label(goal):
    metric = goal['metric']
    inductance = metric in ('L', 'Lp', 'Ls')
    frequency = goal['frequency_hz'] / 1e9
    direction = goal['direction'].capitalize()
    if direction == 'Target':
        return '%s error%s   |   Target %.5g%s   |   %.6g GHz' % (
            metric, ' (pH)' if inductance else '', goal['target'], ' nH' if inductance else '', frequency)
    return '%s%s   |   %.6g GHz   |   %s' % (metric, ' (nH)' if inductance else '', frequency, direction)


def _plot(draw, box, rows, goal, pareto, fonts):
    heading, small = fonts
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=8, fill=PANEL, outline=GRID)
    label = _goal_label(goal)
    draw.text((left + 15, top + 8), label, fill=INK, font=heading)
    measurements = [(row, _measurement(row, goal)) for row in rows]
    if goal['direction'] == 'target':
        scale = 1000 if goal['metric'] in ('L', 'Lp', 'Ls') else 1
        measurements = [(row, abs(value - goal['target']) * scale if value is not None else None)
                        for row, value in measurements]
    values = [value for _, value in measurements if value is not None]
    if not values:
        draw.text(((left + right) / 2, (top + bottom) / 2), 'No measured values at this frequency',
                  fill=MUTED, font=small, anchor='mm')
        return

    x0, x1 = left + 76, right - 25
    y0, y1 = top + 45, bottom - 31
    first, last = rows[0]['index'], rows[-1]['index']
    low, high = min(values), max(values)
    padding = (high - low) * .08 if high > low else max(abs(low) * .05, .01)
    low, high = low - padding, high + padding
    if goal['direction'] == 'target':
        low = max(0, low)

    def point(index, value):
        x = x0 + (index - first) / (last - first) * (x1 - x0) if first != last else (x0 + x1) / 2
        y = y1 - (value - low) / (high - low) * (y1 - y0)
        return x, y

    for i in range(4):
        value = low + (high - low) * i / 3
        _, y = point(first, value)
        draw.line((x0, y, x1, y), fill=GRID)
        draw.text((x0 - 7, y), '%.4g' % value, fill=MUTED, font=small, anchor='rm')
    ticks = sorted({first + round(i * (last - first) / 4) for i in range(5)})
    for index in ticks:
        x, _ = point(index, low)
        draw.line((x, y0, x, y1), fill=GRID)
        draw.text((x, y1 + 10), str(index), fill=MUTED, font=small, anchor='mt')

    previous = None
    best = None
    best_points = []
    first_generation = rows[0].get('generation')
    first_best = None
    better = max if goal['direction'] == 'maximize' else min
    points = []
    for row, value in measurements:
        index = row['index']
        if best is not None:
            best_points.append(point(index, best))
        if value is None:
            previous = None
            continue
        xy = point(index, value)
        if previous is not None and index == previous[0] + 1:
            draw.line((previous[1], xy), fill=TRACE, width=2)
        previous = (index, xy)
        on_front = str(row['id']) in pareto and row.get('feasible') is True
        feasibility = row.get('feasible')
        if on_front:
            color = PARETO
        elif feasibility is True:
            color = FEASIBLE
        elif feasibility is False:
            color = OUTSIDE
        else:
            color = MUTED
        if feasibility is True:
            best = value if best is None else better(best, value)
            best_points.append(point(index, best))
            if first_generation is not None and row.get('generation') == first_generation:
                first_best = best
        points.append((xy, color, 5 if on_front else 3))
    if len(best_points) > 1:
        for start, end in zip(best_points, best_points[1:]):
            draw.line((start, end), fill=BEST, width=2)
    note = '%d / %d measured' % (len(values), len(rows))
    if first_best is not None:
        note = 'First gen best %.4g   /   Run best %.4g' % (first_best, best)
    if draw.textlength(label, font=heading) + draw.textlength(note, font=small) + 52 <= right - left:
        draw.text((right - 18, top + 12), note, fill=MUTED, font=small, anchor='rt')
    for (x, y), color, radius in sorted(points, key=lambda item: item[2]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def render_results(rows, config, size=(1280, 720), pareto_ids=()):
    """Return one RGB image using SNP measurements and original candidate indices."""
    from video_images import image_modules

    Image, ImageDraw, ImageFont = image_modules()

    rows = sorted(rows, key=lambda row: row['index'])
    goals = config.get('objectives') or []
    image = Image.new('RGB', (1280, 720), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title = _font(ImageFont, 34, bold=True)
    body = _font(ImageFont, 17)
    heading = _font(ImageFont, 17, bold=True)
    small = _font(ImageFont, 13)
    draw.text((32, 24), 'Optimization results', fill=INK, font=title)
    counts = ['%s %s' % (sum(row.get('status') == status for row in rows), status)
              for status in ('completed', 'failed', 'rejected', 'cancelled')]
    draw.text((34, 72), '   /   '.join(['%s candidates' % len(rows)] + counts), fill=MUTED, font=body)
    draw.text((34, 688), 'Horizontal axis   Candidate index', fill=MUTED, font=body)
    draw.line((349, 696, 376, 696), fill=BEST, width=2)
    draw.text((386, 686), 'Best feasible so far', fill=INK, font=body)
    legend = [('Pareto', PARETO), ('Feasible', FEASIBLE), ('Outside constraints', OUTSIDE), ('Feasibility unknown', MUTED)]
    x = 585
    for label, color in legend:
        draw.ellipse((x, 692, x + 8, 700), fill=color)
        draw.text((x + 14, 686), label, fill=INK, font=body)
        x += 34 + draw.textlength(label, font=body)

    if not rows or not goals:
        draw.text((640, 390), 'No optimization objective data', fill=MUTED, font=heading, anchor='mm')
    else:
        columns = math.ceil(len(goals) / 3)
        panel_rows = math.ceil(len(goals) / columns)
        cell_width, cell_height = 1228 / columns, 564 / panel_rows
        pareto = {str(ident) for ident in pareto_ids}
        for i, goal in enumerate(goals):
            row, column = divmod(i, columns)
            left, top = 32 + column * cell_width, 107 + row * cell_height
            box = (left, top, left + cell_width - 12, top + cell_height - 10)
            _plot(draw, box, rows, goal, pareto, (heading, small))
    return image if size == image.size else image.resize(size, Image.LANCZOS)
