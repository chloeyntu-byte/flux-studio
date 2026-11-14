"""Discrete EMX search planning and scoring; no simulation or UI side effects."""
import copy
import itertools
import math
import re
import random

_MAX_AXIS_POINTS = 10000


def _finite(value, label):
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('%s must be a finite number' % label)
    if not math.isfinite(value):
        raise ValueError('%s must be a finite number' % label)
    return value


def _descriptor_value(value, field):
    kind = str(field.get('type') or ('enum' if field.get('options') or field.get('choices') else 'float')).lower()
    if value is None:
        if not field.get('optional'):
            raise ValueError('%s is not optional' % field['name'])
        return None
    if kind in ('bool', 'boolean'):
        if not isinstance(value, bool):
            raise ValueError('%s requires a boolean' % field['name'])
        return value
    if kind in ('enum', 'choice', 'select', 'string', 'str'):
        options = field.get('options') or field.get('choices')
        if options and value not in [x.get('id', x.get('value')) if isinstance(x, dict) else x for x in options]:
            raise ValueError('%s is not an allowed choice' % field['name'])
        return value
    number = _finite(value, field['name'])
    if kind in ('int', 'integer') or field.get('integer'):
        if not number.is_integer():
            raise ValueError('%s requires integer values' % field['name'])
        number = int(number)
    lower = field.get('minimum', field.get('min'))
    upper = field.get('maximum', field.get('max'))
    if lower is not None and number < float(lower) - 1e-10 * max(1, abs(float(lower))):
        raise ValueError('%s is below its minimum' % field['name'])
    if upper is not None and number > float(upper) + 1e-10 * max(1, abs(float(upper))):
        raise ValueError('%s exceeds its maximum' % field['name'])
    increment = field.get('value_step')
    if increment is not None:
        increment = _finite(increment, 'field step')
        if increment <= 0:
            raise ValueError('field step must be positive')
        origin = float(field.get('grid_origin', lower if lower is not None else 0))
        pos = (number - origin) / increment
        if abs(pos - round(pos)) > 1e-8 * max(1, abs(pos)):
            raise ValueError('%s is off its permitted grid' % field['name'])
    return number


def _axis_values(spec, field):
    sampling = spec.get('sampling', 'linear')
    if sampling == 'choices':
        raw = spec.get('values')
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValueError('choices require at least one value')
        if len(raw) > _MAX_AXIS_POINTS:
            raise ValueError('axis is too large')
    elif sampling in ('linear', 'log', 'step'):
        lo = _finite(spec.get('minimum'), 'minimum')
        hi = _finite(spec.get('maximum'), 'maximum')
        if lo > hi:
            raise ValueError('minimum exceeds maximum')
        if sampling == 'step':
            step = _finite(spec.get('step'), 'step')
            if step <= 0:
                raise ValueError('step must be positive')
            n = int(math.floor((hi - lo) / step + 1e-10)) + 1
            if n > _MAX_AXIS_POINTS:
                raise ValueError('axis is too large')
            raw = [lo + i * step for i in range(n)]
        else:
            count = spec.get('count')
            if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > _MAX_AXIS_POINTS:
                raise ValueError('count must be a positive bounded integer')
            if count == 1 and lo != hi:
                raise ValueError('one point requires equal bounds')
            if sampling == 'log' and (lo <= 0 or hi <= 0):
                raise ValueError('log bounds must be positive')
            if count == 1:
                raw = [lo]
            elif sampling == 'linear':
                raw = [lo + (hi - lo) * i / (count - 1) for i in range(count)]
            else:
                raw = [math.exp(math.log(lo) + (math.log(hi) - math.log(lo)) * i / (count - 1)) for i in range(count)]
                raw[0], raw[-1] = lo, hi
    else:
        raise ValueError('unsupported sampling: %s' % sampling)
    result = [_descriptor_value(value, field) for value in raw]
    if len(set(result)) != len(result):
        raise ValueError('axis has duplicate values')
    return result


class SearchSpace:
    def __init__(self, fields, axis_specs):
        descriptors = {field['name']: field for field in fields}
        if len(descriptors) != len(fields):
            raise ValueError('duplicate field descriptors')
        self._axes = []
        seen = set()
        groups = {}
        for spec in axis_specs:
            if not spec.get('enabled', True):
                continue
            name = spec.get('name')
            if name in seen or name not in descriptors:
                raise ValueError('duplicate or unknown field: %s' % name)
            seen.add(name)
            values = _axis_values(spec, descriptors[name])
            group = str(spec.get('group') or '')
            signature = (spec.get('sampling', 'linear'), tuple(spec.get('values') or [])) if spec.get('sampling') == 'choices' else (spec.get('sampling', 'linear'), spec.get('minimum'), spec.get('maximum'), spec.get('count'), spec.get('step'))
            if group and group in groups:
                dimension, prior_signature = groups[group]
                if signature != prior_signature or len(values) != len(self._axes[dimension][0][1]):
                    raise ValueError('paired axes must have identical sampling and points')
                self._axes[dimension].append((name, values))
            else:
                dimension = len(self._axes)
                self._axes.append([(name, values)])
                if group:
                    groups[group] = (dimension, signature)
        self.dimensions = len(self._axes)
        self.lengths = tuple(len(axis[0][1]) for axis in self._axes)
        self.size = 1
        for length in self.lengths:
            self.size *= length

    def decode(self, coordinates):
        if len(coordinates) != self.dimensions:
            raise ValueError('coordinate dimension mismatch')
        result = {}
        for axis, index in zip(self._axes, coordinates):
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(axis[0][1]):
                raise ValueError('coordinate out of bounds')
            for name, values in axis:
                result[name] = values[index]
        return result

    def iter_sweep(self):
        for coordinate in itertools.product(*(range(n) for n in self.lengths)):
            yield self.decode(coordinate)


def apply_values(design, flat_values):
    result = copy.deepcopy(design)
    for name, value in flat_values.items():
        if name == 'structure':
            result['structure'] = value
        elif name.startswith('layers.'):
            result.setdefault('layers', {})[name[7:]] = value
        else:
            result.setdefault('parameters', {})[name] = value
    return result


def evaluate_response(response, objectives, constraints):
    def measurement(spec):
        freq = _finite(spec.get('frequency_hz'), 'frequency')
        if freq <= 0:
            raise ValueError('frequency must be positive')
        matches = [row for row in response if abs(_finite(row.get('frequency_hz'), 'response frequency') - freq) <= max(abs(freq) * 1e-12, 1e-12)]
        if len(matches) != 1:
            raise ValueError('frequency %s is missing or ambiguous' % freq)
        key = metric_key(spec)
        return _finite(matches[0].get(key), 'metric %s' % key)
    scores, raw = [], []
    for spec in objectives:
        value = measurement(spec)
        scale = _finite(spec.get('scale', 1), 'scale')
        if scale <= 0:
            raise ValueError('scale must be positive')
        direction = spec.get('direction')
        if direction == 'minimize': score = value / scale
        elif direction == 'maximize': score = -value / scale
        elif direction == 'target': score = abs(value - _finite(spec.get('target'), 'target')) / scale
        else: raise ValueError('unsupported objective direction')
        scores.append(score); raw.append(value)
    violation = 0.0
    for spec in constraints:
        value = measurement(spec)
        scale = _finite(spec.get('scale', 1), 'scale')
        if scale <= 0: raise ValueError('scale must be positive')
        op, limit = spec.get('operator'), spec.get('limit')
        if op == '<=': v = max(0, value - _finite(limit, 'limit'))
        elif op == '>=': v = max(0, _finite(limit, 'limit') - value)
        elif op == 'between':
            if not isinstance(limit, (list, tuple)) or len(limit) != 2: raise ValueError('between requires two limits')
            lo, hi = (_finite(x, 'limit') for x in limit)
            if lo > hi: raise ValueError('invalid between limits')
            v = max(0, lo - value, value - hi)
        else: raise ValueError('unsupported constraint')
        violation += v / scale
    if not math.isfinite(violation) or any(not math.isfinite(x) for x in scores): raise ValueError('nonfinite score')
    return {'objectives': scores, 'objective_values': raw, 'violation': violation, 'feasible': violation == 0}


def _dominates(a, b):
    if a['violation'] != b['violation']:
        return a['violation'] < b['violation']
    return all(x <= y for x, y in zip(a['objectives'], b['objectives'])) and any(x < y for x, y in zip(a['objectives'], b['objectives']))


def pareto(rows):
    eligible = [r for r in rows if r.get('ok', r.get('status') == 'completed') and r.get('violation', float('inf')) == 0 and r.get('objectives') and all(math.isfinite(float(v)) for v in r['objectives'])]
    if len({len(r['objectives']) for r in eligible}) > 1: raise ValueError('objective dimension mismatch')
    return [r for r in eligible if not any(_dominates(other, r) for other in eligible if other is not r)]


def _fronts(rows):
    rest, result = list(rows), []
    while rest:
        front = [r for r in rest if not any(_dominates(o, r) for o in rest if o is not r)]
        result.append(front)
        ids = {id(r) for r in front}
        rest = [r for r in rest if id(r) not in ids]
    return result


def _crowding(front):
    distance = {id(r): 0.0 for r in front}
    for j in range(len(front[0]['objectives'])):
        ordered = sorted(front, key=lambda r: r['objectives'][j])
        distance[id(ordered[0])] = distance[id(ordered[-1])] = float('inf')
        span = ordered[-1]['objectives'][j] - ordered[0]['objectives'][j]
        if span:
            for i in range(1, len(ordered)-1):
                distance[id(ordered[i])] += (ordered[i+1]['objectives'][j] - ordered[i-1]['objectives'][j]) / span
    return distance


def _select(rows, count):
    selected = []
    for front in _fronts(rows):
        d = _crowding(front)
        selected.extend(sorted(front, key=lambda r: -d[id(r)])[:count-len(selected)])
        if len(selected) == count: break
    return selected


def _solve(matrix, vector):
    a = [row[:] + [v] for row, v in zip(matrix, vector)]
    n = len(a)
    for i in range(n):
        p = max(range(i, n), key=lambda j: abs(a[j][i]))
        a[i], a[p] = a[p], a[i]
        if abs(a[i][i]) < 1e-15: raise ValueError('singular Gaussian process')
        pivot = a[i][i]
        for k in range(i, n+1): a[i][k] /= pivot
        for j in range(n):
            if j != i:
                factor = a[j][i]
                for k in range(i, n+1): a[j][k] -= factor*a[i][k]
    return [a[i][-1] for i in range(n)]


class Optimizer:
    """Discrete NSGA-II, DE/rand/1/bin, or Gaussian-process LCB search."""
    def __init__(self, space, algorithm='nsga2', population=12, generations=3, seed=1, **advanced):
        if not space.dimensions or space.size < 2: raise ValueError('optimization requires variable axes')
        algorithm = {'de': 'differential_evolution', 'bayesian': 'bayesian_optimization'}.get(algorithm, algorithm)
        if algorithm not in ('nsga2', 'differential_evolution', 'bayesian_optimization'): raise ValueError('unsupported algorithm')
        if isinstance(population, bool) or not isinstance(population, int) or population < 2: raise ValueError('population must be at least 2')
        if isinstance(generations, bool) or not isinstance(generations, int) or generations < 1: raise ValueError('generations must be positive')
        self.space, self.algorithm, self.population, self.generations = space, algorithm, population, generations
        self.rng, self.advanced = random.Random(seed), advanced
        self.seen, self.history, self.parents = set(), [], []
        self.generation, self.pending = 0, None
        self.objective_count = None

    def _coords(self, vector):
        return tuple(min(n-1, max(0, int(round(v*(n-1))))) for v,n in zip(vector, self.space.lengths))

    def _vector(self, coord):
        return [i/(n-1) if n > 1 else 0.0 for i,n in zip(coord, self.space.lengths)]

    def _random(self): return tuple(self.rng.randrange(n) for n in self.space.lengths)

    def _bo_order(self, coords):
        rows = self.history[-80:]
        vectors = [self._vector(r['_coord']) for r in rows]
        # Constraint violation dominates objective in a bounded transformed acquisition target.
        scores = [math.atan(r['objectives'][0])/math.pi + (1.0 + math.atan(r['violation'])/math.pi if r['violation'] > 0 else 0.0) for r in rows]
        length = float(self.advanced.get('kernel_length_scale', 0.35))
        exploration = float(self.advanced.get('acquisition_exploration', 2.0))
        if not math.isfinite(length) or length <= 0 or not math.isfinite(exploration) or exploration < 0: raise ValueError('invalid Bayesian parameters')
        kernel = lambda a,b: math.exp(-sum((x-y)**2 for x,y in zip(a,b))/(2*length*length))
        matrix = [[kernel(a,b) + (1e-6 if i == j else 0) for j,b in enumerate(vectors)] for i,a in enumerate(vectors)]
        alpha = _solve(matrix, scores)
        def acq(c):
            v = self._vector(c); k = [kernel(v, t) for t in vectors]
            mean = sum(x*y for x,y in zip(k, alpha))
            var = max(0.0, 1.0 - sum(x*y for x,y in zip(k, _solve(matrix, k))))
            return mean - exploration*math.sqrt(var)
        return sorted(coords, key=acq)

    def ask(self):
        if self.pending is not None: raise ValueError('tell must follow ask')
        if self.generation >= self.generations or len(self.seen) >= self.space.size: return []
        wanted = min(self.population, self.space.size-len(self.seen))
        candidates = []
        targets = {}
        attempts = max(100, wanted*50)
        while len(candidates) < wanted and attempts:
            attempts -= 1
            target = None
            if not self.parents:
                coord = self._random()
            elif self.algorithm == 'nsga2':
                fronts = _fronts(self.parents); rank = {id(r): i for i,f in enumerate(fronts) for r in f}
                crowd = {}; [crowd.update(_crowding(f)) for f in fronts]
                def tournament():
                    a,b = self.rng.choice(self.parents), self.rng.choice(self.parents)
                    return min((a,b), key=lambda r:(rank[id(r)], -crowd[id(r)]))
                a,b = self._vector(tournament()['_coord']), self._vector(tournament()['_coord'])
                vector = []
                for x,y in zip(a,b):
                    beta = (2*self.rng.random())**(1/16.0)
                    v = 0.5*((1+beta)*x+(1-beta)*y) if self.rng.random()<0.9 else x
                    if self.rng.random() < 1/self.space.dimensions: v += self.rng.gauss(0,0.12)
                    vector.append(min(1,max(0,v)))
                coord = self._coords(vector)
            elif self.algorithm == 'differential_evolution' and len(self.parents) >= 4:
                target = self.rng.choice(self.parents)
                pool = [r for r in self.parents if r is not target]
                a,b,c = [self._vector(r['_coord']) for r in self.rng.sample(pool,3)]
                base = self._vector(target['_coord']); forced = self.rng.randrange(self.space.dimensions)
                weight, rate = float(self.advanced.get('differential_weight',0.8)), float(self.advanced.get('crossover_rate',0.9))
                if not 0 < weight <= 2 or not 0 <= rate <= 1: raise ValueError('invalid differential evolution parameters')
                coord = self._coords([min(1,max(0,a[i]+weight*(b[i]-c[i]))) if i == forced or self.rng.random()<rate else base[i] for i in range(len(base))])
            else:
                coord = self._random()
            if coord not in self.seen and coord not in candidates:
                candidates.append(coord)
                if target is not None: targets[coord] = target
        if len(candidates) < wanted:
            # Walk cyclic mixed-radix indices only until the remaining requested
            # points are found; random.randrange supports arbitrary-size integers.
            index = self.rng.randrange(self.space.size)
            while len(candidates) < wanted:
                residue, reverse = index, []
                for length in reversed(self.space.lengths):
                    residue, digit = divmod(residue, length)
                    reverse.append(digit)
                coord = tuple(reversed(reverse))
                if coord not in self.seen and coord not in candidates:
                    candidates.append(coord)
                index = (index + 1) % self.space.size
        if self.algorithm == 'bayesian_optimization' and self.history:
            pool = list(candidates)
            for _ in range(min(200, self.space.size)):
                coord = self._random()
                if coord not in self.seen and coord not in pool: pool.append(coord)
            candidates = self._bo_order(pool)[:wanted]
        self.pending = candidates
        self._targets = targets
        self.seen.update(candidates)
        self.generation += 1
        return [self.space.decode(c) for c in candidates]

    def tell(self, rows):
        if self.pending is None or len(rows) != len(self.pending): raise ValueError('tell rows do not match pending batch')
        valid = []
        for coord, row in zip(self.pending, rows):
            if row.get('values') != self.space.decode(coord): raise ValueError('tell rows must preserve ask order and values')
            if not row.get('ok'): continue
            scores = [_finite(v, 'objective') for v in row.get('objectives', [])]
            if not scores: raise ValueError('at least one objective required')
            if self.objective_count is None: self.objective_count = len(scores)
            if len(scores) != self.objective_count: raise ValueError('objective dimension mismatch')
            if self.algorithm != 'nsga2' and len(scores) != 1: raise ValueError('algorithm supports one objective only')
            violation = _finite(row.get('violation'), 'violation')
            if violation < 0: raise ValueError('negative violation')
            valid.append({'_coord': coord, 'objectives': scores, 'violation': violation})
        self.history.extend(valid)
        if valid and self.algorithm == 'differential_evolution' and self.parents:
            survivors = list(self.parents)
            unmatched = []
            for trial in valid:
                target = self._targets.get(trial['_coord'])
                if target is None:
                    unmatched.append(trial)
                elif target in survivors and _dominates(trial, target):
                    survivors[survivors.index(target)] = trial
            self.parents = _select(survivors + unmatched, self.population)
        elif valid:
            self.parents = _select(self.parents + valid, self.population)
        self.pending = None


# Metrics that can be evaluated on explicitly chosen pins, and how many pins
# each needs: a differential pair (+, -) for L/Q, two pairs for coupling k.
PIN_METRIC_COUNTS = {'L': 2, 'Q': 2, 'Lp': 2, 'Ls': 2, 'Qp': 2, 'Qs': 2, 'k': 4}
_KEY = re.compile(r'^([A-Za-z]+)\[([A-Za-z][A-Za-z0-9_]*(?:,[A-Za-z][A-Za-z0-9_]*)*)\]$')


def metric_key(spec):
    """Response column for a goal: ``Lp`` or ``Lp[pin_1,pin_2]``."""
    pins = spec.get('pins')
    if not pins:
        return spec.get('metric')
    return '%s[%s]' % (spec.get('metric'), ','.join(pins))


def parse_metric_key(text):
    """``'Lp[a,b]'`` -> ``('Lp', ['a', 'b'])``; plain names -> ``(name, None)``."""
    text = str(text).strip().replace(' ', '')
    match = _KEY.match(text)
    if not match:
        return text, None
    return match.group(1), match.group(2).split(',')


def check_pins(metric, pins, available=None):
    """Validate an explicit pin assignment; returns the pin list."""
    if metric not in PIN_METRIC_COUNTS:
        raise ValueError('Metric %s cannot be assigned to pins.' % metric)
    count = PIN_METRIC_COUNTS[metric]
    if not isinstance(pins, (list, tuple)) or len(pins) != count or not all(isinstance(p, str) and p for p in pins):
        raise ValueError('%s needs %d pins (%s).' % (metric, count, '+, -' if count == 2 else 'pair 1 +, -, pair 2 +, -'))
    if count == 2 and pins[0] == pins[1]:
        raise ValueError('%s needs two different pins.' % metric)
    if count == 4 and (pins[0] == pins[1] or pins[2] == pins[3]):
        raise ValueError('k needs two different pins in each pair.')
    if available is not None:
        missing = [p for p in pins if p not in available]
        if missing:
            raise ValueError('Pins %s are not in the layout (available: %s).' % (', '.join(missing), ', '.join(available)))
    return list(pins)


def metric_specs(objectives, constraints):
    """Distinct pin-assigned metrics used by the goals, for network summaries."""
    result, seen = [], set()
    for spec in list(objectives or []) + list(constraints or []):
        if spec.get('pins'):
            key = metric_key(spec)
            if key not in seen:
                seen.add(key)
                result.append(dict(key=key, metric=spec['metric'], pins=list(spec['pins'])))
    return result
