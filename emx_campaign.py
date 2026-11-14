"""Owned Flux EMX campaign coordinator; native OA/export serialized, solver bounded."""
import cell_lifecycle
import copy
import contextlib
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import queue
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import backend
import emx_adapter
import emx_search

_NATIVE_LOCK = threading.Lock()
_SOLVER_SLOTS = threading.BoundedSemaphore(2)


def _atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('x', encoding='utf-8') as out:
        json.dump(value, out, indent=2, sort_keys=True, allow_nan=False)
        out.write('\n')
    if os.geteuid() == 0:
        owner = path.parent.stat()
        os.chown(str(temporary), owner.st_uid, owner.st_gid)
    os.replace(str(temporary), str(path))


def _merge_warnings(*groups):
    result = []
    for group in groups:
        for warning in group or []:
            if warning not in result:
                result.append(copy.deepcopy(warning))
    return result


def _counts(rows):
    return {state: sum(row.get('status') == state for row in rows)
            for state in ('completed', 'failed', 'rejected', 'cancelled')}


def _validate_goals(objectives, constraints, supported, pin_names=None, require_pins=False):
    def number(value, field, positive=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (positive and value <= 0):
            raise ValueError('{} must be a {}finite number.'.format(field, 'positive ' if positive else ''))
        return value
    for kind, specs in (('objective', objectives), ('constraint', constraints)):
        for spec in specs:
            metric = spec.get('metric')
            if not isinstance(metric, str) or not metric:
                raise ValueError('{} metric is required.'.format(kind))
            if spec.get('pins'):
                emx_search.check_pins(metric, spec['pins'], pin_names)
            elif require_pins:
                raise ValueError('The layout uses custom leg pins; assign pins to {} {}.'.format(kind, metric))
            if supported is not None and metric not in supported and not spec.get('pins'):
                raise ValueError('{} metric {} is unavailable for this model; choose {}.'.format(kind, metric, ', '.join(supported)))
            number(spec.get('frequency_hz'), kind + ' frequency', positive=True)
            number(spec.get('scale', 1), kind + ' scale', positive=True)
            if kind == 'objective':
                if spec.get('direction') not in ('minimize', 'maximize', 'target'):
                    raise ValueError('Objective direction must be minimize, maximize or target.')
                if spec['direction'] == 'target':
                    number(spec.get('target'), 'objective target')
            else:
                operator = spec.get('operator')
                if operator not in ('<=', '>=', 'between'):
                    raise ValueError('Constraint operator must be <=, >= or between.')
                limit = spec.get('limit')
                if operator == 'between':
                    if not isinstance(limit, (list, tuple)) or len(limit) != 2:
                        raise ValueError('Between constraint requires two limits.')
                    low, high = (number(x, 'constraint limit') for x in limit)
                    if low > high:
                        raise ValueError('Between constraint limits must be ordered.')
                else:
                    number(limit, 'constraint limit')


def _metrics(summary):
    rows = []
    for sample in summary['samples']:
        row = {'frequency_hz': sample['frequency_hz']}
        for key in ('requested_frequency_hz', 'reported_frequency_hz', 'frequency_token', 'frequency_resolution_hz'):
            if key in sample:
                row[key] = sample[key]
        windings = sample['windings']
        for name, prefix in (('L', ''), ('P', 'p'), ('S', 's')):
            if name in windings:
                winding = windings[name]
                if winding.get('inductance_h') is not None:
                    row['L' + prefix] = winding['inductance_h'] * 1e9
                if winding.get('q') is not None:
                    row['Q' + prefix] = winding['q']
        if sample.get('coupling_k') is not None:
            row['k'] = sample['coupling_k']
        for key, value in (sample.get('pin_metrics') or {}).items():
            if value is not None:
                row[key] = value
        rows.append(row)
    return rows


def _slug(text, limit=40):
    return (re.sub(r'[^A-Za-z0-9]+', '_', str(text)).strip('_') or 'value')[:limit]


def _snp_name(source, row, port_count):
    design = row['design']
    model = _slug(design.get('model', 'device'), 25)
    fields = source.get('fields', [])
    pieces = [model]
    if any(field.get('name') == 'structure' for field in fields):
        structure = design.get('structure')
        if isinstance(structure, str) and structure:
            pieces.append(_slug(structure, 24))
    if row.get('values'):
        pairs = sorted(row['values'].items())
    else:
        parameters = design.get('parameters', {})
        pairs = [(field.get('name'), parameters.get(field.get('name'))) for field in fields
                 if field.get('part') in ('body', 'legs') and field.get('name') in parameters
                 and (isinstance(parameters.get(field.get('name')), (int, float)) and not isinstance(parameters.get(field.get('name')), bool)
                      or isinstance(parameters.get(field.get('name')), str) and (field.get('type') == 'enum' or field.get('options')))]
    tokens = []
    length = 0
    for name, value in pairs:
        if isinstance(value, float) and not math.isfinite(value):
            continue
        token = _slug(name, 32) + _slug(format(value, '.10g') if isinstance(value, (int, float)) and not isinstance(value, bool) else value, 24)
        if length + len(token) + bool(tokens) > 75:
            break
        tokens.append(token)
        length += len(token) + (1 if len(tokens) > 1 else 0)
    if tokens:
        pieces.append('_'.join(tokens))
    pieces.append(row['id'])
    return '{}.s{}p'.format('_'.join(pieces), port_count)


class Campaign:
    def __init__(self, source, config):
        self.source = copy.deepcopy(source)
        self.config = copy.deepcopy(config)
        self.run_id = uuid.uuid4().hex
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self._thread = None
        self._active = False
        self._started = False
        self._start_lock = threading.Lock()
        self._optimizer = None
        self._preflight_warnings = []
        self.rows = []
        self.output_dir = None
        self.space = emx_search.SearchSpace(self.source['fields'], self.config.get('axes', []))
        if self.config.get('mode', 'sweep') not in ('sweep', 'optimize'):
            raise ValueError('Unknown campaign mode.')
        concurrency = self.config.get('concurrency', 2)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 2:
            raise ValueError('Campaign concurrency must be 1 or 2.')
        self.concurrency = concurrency
        _validate_goals(self.config.get('objectives', []), self.config.get('constraints', []),
                        self.source.get('definition', {}).get('emx_metrics'),
                        self.source.get('pins'), bool(self.source.get('custom_pins')))
        self.emx = copy.deepcopy(self.config.get('emx', {}))
        self.settings = emx_adapter.resolve_settings(self.emx)
        frequencies = self.emx.get('frequencies_hz', [])
        if not isinstance(frequencies, (list, tuple)) or not frequencies:
            raise ValueError('Provide at least one EMX frequency.')
        if any(isinstance(f, bool) or not isinstance(f, (float, int)) or not math.isfinite(f) or f <= 0 for f in frequencies):
            raise ValueError('EMX frequencies must be finite and positive.')
        if len(frequencies) > 256:
            raise ValueError('At most 256 EMX frequencies are supported.')
        for spec in self.config.get('objectives', []) + self.config.get('constraints', []):
            freq = spec['frequency_hz']
            matches = [f for f in frequencies if abs(f - freq) <= max(abs(freq) * 1e-12, 1e-12)]
            if len(matches) != 1:
                raise ValueError('Objective or constraint frequency {} Hz is missing or ambiguous in EMX settings. Available frequencies (Hz): {}. Change the EMX frequencies or the objective/constraint.'.format(freq, list(frequencies)))
        self.settings['frequencies_hz'] = list(frequencies)
        if self.config.get('mode') == 'optimize' and not self.config.get('objectives'):
            raise ValueError('Optimization requires at least one objective.')
        algorithm = self.config.get('optimizer', {}).get('algorithm', 'nsga2')
        if self.config.get('mode') == 'optimize' and algorithm != 'nsga2' and len(self.config.get('objectives', [])) != 1:
            raise ValueError('The selected optimizer supports one objective only.')
        if self.config.get('mode') == 'optimize':
            options = self.config.get('optimizer', {})
            seed = options.get('seed', 1)
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise ValueError('Optimizer seed must be an integer.')
            if algorithm in ('de', 'differential_evolution') and (isinstance(options.get('population', 12), bool) or not isinstance(options.get('population', 12), int) or options.get('population', 12) < 4):
                raise ValueError('Differential evolution population must be at least 4.')
            self._optimizer = emx_search.Optimizer(self.space, **options)
            if algorithm in ('de', 'differential_evolution') and self.space.size < 4:
                self._preflight_warnings.append('Fewer than four unique candidates exist; all available candidates can be evaluated but a differential-evolution generation cannot be formed.')

    @property
    def is_running(self):
        return self._active or bool(self._thread and self._thread.is_alive())

    def _event(self, kind, **values):
        self.events.put(dict(type=kind, run_id=self.run_id, **values))

    def start(self):
        with self._start_lock:
            if self._started:
                raise RuntimeError('Campaign has already started.')
            self._started = True
            self._active = True
            self._thread = threading.Thread(target=self._run_impl, name='flux-emx-' + self.run_id[:8], daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self._active = False
                raise
        return self

    def run(self):
        with self._start_lock:
            if self._started:
                raise RuntimeError('Campaign has already started.')
            self._started = True
            self._active = True
        return self._run_impl()

    @contextlib.contextmanager
    def _worker_pool(self):
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            try:
                yield pool
            except BaseException:
                # Signal active and queued work before Executor.__exit__ waits.
                self.cancel_event.set()
                raise

    def cancel(self):
        self.cancel_event.set()

    def _open_output(self):
        parent = emx_adapter._run_path(self.config['output_dir'])
        if parent.exists():
            if not parent.is_dir():
                raise ValueError('Campaign output parent is not a directory.')
            directory = parent / ('flux_emx_' + self.run_id[:12])
        else:
            directory = parent
            parent = directory.parent
        user = pwd.getpwnam(self.source['runtime']['user'])
        if user.pw_uid == 0 or os.geteuid() not in (0, user.pw_uid):
            raise ValueError('Campaign must belong to the native non-root user.')
        ancestor = parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if (not ancestor.is_dir() or not emx_adapter._native_can_access(ancestor, user, os.X_OK)
                or not emx_adapter._native_can_access(ancestor, user, os.W_OK)):
            raise ValueError('Campaign output ancestor is not writable and traversable by the native user: {}.'.format(ancestor))
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open('/', flags)
        try:
            for part in ancestor.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            components = list(directory.relative_to(ancestor).parts)
            if not components:
                raise ValueError('Campaign output directory must be newly created.')
            for part in components:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                os.fchmod(descriptor, 0o700)
                if os.geteuid() == 0:
                    os.fchown(descriptor, user.pw_uid, user.pw_gid)
            os.mkdir('results', 0o700, dir_fd=descriptor)
            child = os.open('results', flags, dir_fd=descriptor)
            try:
                os.fchmod(child, 0o700)
                if os.geteuid() == 0:
                    os.fchown(child, user.pw_uid, user.pw_gid)
            finally:
                os.close(child)
        finally:
            os.close(descriptor)
        self.output_dir = str(directory)
        public_config = copy.deepcopy(self.config)
        public_config['emx'] = {k: v for k, v in self.emx.items() if k != 'environment'}
        _atomic(directory / 'campaign.json', dict(schema='flux.emx.campaign.v1', run_id=self.run_id,
                identity=self.source['identity'], design=self.source['design'], config=public_config,
                effective_settings=self.settings, warnings=self._preflight_warnings,
                metric_definition='Differential winding impedance with other signals open; L in nH, Q dimensionless, k signed.'))

    def _save(self):
        directory = Path(self.output_dir)
        _atomic(directory / 'index.json', self.rows)
        temp = directory / ('index.csv.tmp-' + uuid.uuid4().hex)
        with temp.open('x', newline='', encoding='utf-8') as out:
            fields = ['id', 'index', 'generation', 'status', 'values', 'source', 'job_dir', 'log_path', 'snp_path', 'metrics', 'objectives', 'objective_values', 'violation', 'feasible', 'error', 'errors', 'network', 'retention']
            writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            for row in self.rows:
                writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False, sort_keys=True) if isinstance(row.get(key), (dict, list)) else row.get(key) for key in fields})
        if os.geteuid() == 0:
            owner = directory.stat()
            os.chown(str(temp), owner.st_uid, owner.st_gid)
        os.replace(str(temp), str(directory / 'index.csv'))

    def _prepare(self, values, index, generation):
        started = time.monotonic()
        identifier = 'c{:05d}_{}'.format(index, uuid.uuid4().hex[:10])
        row = dict(id=identifier, index=index, generation=generation, status='rejected', values=values,
                   design=None, source={}, job_dir=None, log_path=None, snp_path=None, metrics=[],
                   objectives=[], objective_values=[], violation=None, feasible=False, warnings=[], error=None, errors=[], elapsed_seconds=0,
                   retention={'state': 'not_created'})
        job = None
        try:
            if self.cancel_event.is_set():
                row['status'] = 'cancelled'
                return row, None, started
            design = emx_search.apply_values(self.source['design'], values)
            design['cell'] = 'F_{}_{}'.format(_slug(design.get('model', 'device'), 32), identifier)
            row['design'] = design
            self._event('state', stage='preview', message='{}: validating layout'.format(identifier))
            preview = backend.preview_in_context(design, self.source)
            row['warnings'] = _merge_warnings(row['warnings'], preview.get('warnings'))
            if not preview.get('ok'):
                row['errors'] = copy.deepcopy(preview.get('errors', []))
                row['error'] = 'Layout validation failed: ' + str(preview.get('errors', []))
                return row, None, started
            if self.cancel_event.is_set():
                row['status'] = 'cancelled'
                return row, None, started
            self._event('state', stage='generate', message='{}: generating and verifying layout'.format(identifier))
            row['retention'] = dict(state='generation_in_progress', library=design.get('library'), cell=design['cell'])
            generated = backend.generate_in_context(design, self.source)
            row['warnings'] = _merge_warnings(row['warnings'], generated.get('warnings'))
            if not generated.get('ok'):
                row['retention']['state'] = 'generation_unverified'
                row['errors'] = copy.deepcopy(generated.get('errors', []))
                row['error'] = 'Layout generation failed: ' + str(generated.get('errors', []))
                row['log_path'] = generated.get('run_dir')
                return row, None, started
            row['source'] = dict(self.source['identity'], cell=design['cell'], view='layout')
            try:
                row['retention'] = dict(state='pending', handle=cell_lifecycle.prepare(generated, self.source))
            except Exception as exc:
                row['retention'] = dict(state='prepare_failed', error=str(exc), receipt=generated.get('run_dir'))
                row['warnings'].append('Cell cleanup preparation failed: ' + str(exc))
            generation_dir = generated.get('run_dir')
            if generation_dir:
                generation_log = Path(generation_dir) / 'stdout.log'
                row['log_path'] = str(generation_log) if generation_log.is_file() else str(generation_dir)
            if self.cancel_event.is_set():
                row['status'] = 'cancelled'
                return row, None, started
            self._event('state', stage='export', message='{}: verifying source and exporting layout'.format(identifier))
            job_dir = Path(self.output_dir) / identifier
            job = backend.prepare_emx_job(generated, overrides=self.emx, run_dir=job_dir, source_context=self.source)
            job['metric_specs'] = emx_search.metric_specs(self.config.get('objectives', []), self.config.get('constraints', []))
            row['source'] = {k: job['source'][k] for k in ('library', 'cell', 'view', 'technology', 'workspace', 'cds_lib', 'library_path')}
            row['job_dir'] = str(job_dir)
            emx_adapter.export_layout(job, self.source['runtime'], self.cancel_event)
            _atomic(job_dir / 'manual-replay.json', dict(source=job['source'], inputs=job['inputs'], ports=job['ports'], effective_settings=job['effective_settings'], stream_argv=job['stream_argv'], emx_argv=job['emx_argv'], outputs=job['outputs'], instructions='The temporary OA cell is removed after extraction. Regenerate this candidate layout from its saved design before manual replay. Enter the effective settings, process/map and ordered ports shown here; argv records the original native CLI execution.'))
            return row, job, started
        except Exception as exc:
            row['status'] = 'cancelled' if self.cancel_event.is_set() else 'failed'
            row['error'] = str(exc)
            if row.get('retention', {}).get('state') == 'generation_in_progress':
                row['retention']['state'] = 'generation_unverified'
                row['retention']['error'] = 'Generation did not return a verified write receipt; inspect the cell and native logs.'
                row['warnings'].append(row['retention']['error'])
            if job and job.get('stages'):
                row['log_path'] = job['stages'][-1].get('log') or row['log_path']
            return row, None, started

    def _solve(self, row, job, started):
        row['warnings'] = _merge_warnings(row.get('warnings'), job.get('warnings'))
        try:
            if self.cancel_event.is_set():
                row['status'] = 'cancelled'
                row['error'] = 'Cancelled before EMX solver launch.'
                return row
            self._event('state', stage='solver', message='{}: running EMX solver'.format(row['id']))
            emx_adapter.run_job(job, self.source['runtime'], self.cancel_event)
            row['metrics'] = _metrics(job['network_summary'])
            row['warnings'] = _merge_warnings(row['warnings'], job.get('warnings'),
                ['Pin metric %s unavailable: %s' % item for item in sorted((job['network_summary'].get('pin_metric_errors') or {}).items())])
            name = _snp_name(self.source, row, len(job['ports']['signals']))
            destination = Path(self.output_dir) / 'results' / name
            shutil.copyfile(job['outputs']['touchstone'], str(destination))
            if os.geteuid() == 0:
                owner = destination.parent.stat()
                os.chown(str(destination), owner.st_uid, owner.st_gid)
            row['snp_path'] = str(destination)
            import hashlib
            with destination.open('rb') as stream:
                snp_hash = hashlib.sha256(stream.read()).hexdigest()
            row['network'] = dict(ports=copy.deepcopy(job['ports']), frequencies_hz=copy.deepcopy(job['frequencies_hz']), snp_sha256=snp_hash)
            if self.config.get('objectives') or self.config.get('constraints'):
                evaluation = emx_search.evaluate_response(row['metrics'], self.config.get('objectives', []), self.config.get('constraints', []))
                row.update(evaluation)
            else:
                row.update(feasible=True, violation=0.0)
            row['status'] = 'completed'
            if job['outputs'].get('gds') and Path(job['outputs']['gds']).is_file():
                # Only this owned, validated job's reproducible GDS; OA and records remain.
                Path(job['outputs']['gds']).unlink()
                row['warnings'].append('Owned intermediate GDS removed after success.')
        except Exception as exc:
            row['status'] = 'cancelled' if self.cancel_event.is_set() else 'failed'
            row['error'] = str(exc)
        finally:
            stages = job.get('stages', [])
            if stages:
                row['log_path'] = stages[-1].get('log') or row['log_path']
            row['elapsed_seconds'] = round(time.monotonic() - started, 3)
        return row

    def _record_failure(self, row):
        if not row.get('error') or not self.output_dir:
            return
        path = Path(self.output_dir) / (row['id'] + '-setup.log')
        try:
            with path.open('x', encoding='utf-8') as out:
                if os.geteuid() == 0:
                    owner = path.parent.stat()
                    os.fchown(out.fileno(), owner.st_uid, owner.st_gid)
                out.write('Candidate {}: {}\n'.format(row['id'], row['error']))
            row['setup_log_path'] = str(path)
            existing = row.get('log_path')
            if not existing or not Path(existing).is_file():
                row['log_path'] = str(path)
        except OSError:
            row['setup_log_path'] = None
            row['warnings'] = _merge_warnings(row.get('warnings'), ['Could not save the setup error log.'])

    def _global_error_log(self, message):
        if not self.output_dir:
            return None
        path = Path(self.output_dir) / 'setup.log'
        try:
            with path.open('x', encoding='utf-8') as out:
                if os.geteuid() == 0:
                    owner = path.parent.stat()
                    os.fchown(out.fileno(), owner.st_uid, owner.st_gid)
                out.write('Campaign setup or execution failed: {}\n'.format(message))
            return str(path)
        except OSError:
            return None

    def _solve_guarded(self, row, job, started):
        while not self.cancel_event.is_set():
            if _SOLVER_SLOTS.acquire(timeout=.1):
                try:
                    return self._solve(row, job, started)
                finally:
                    _SOLVER_SLOTS.release()
        row.update(status='cancelled', error='Cancelled before solver slot became available.',
                   elapsed_seconds=round(time.monotonic() - started, 3))
        return row

    def _run_impl(self):
        status = 'failed'
        try:
            self._open_output()
            self._event('state', stage='running', message='Campaign started')
            for warning in self._preflight_warnings:
                self._event('log', message=warning)
            mode = self.config.get('mode', 'sweep')
            optimizer = self._optimizer
            sweep = iter(self.space.iter_sweep()) if optimizer is None else None
            total_expected = self.space.size if optimizer is None else min(self.space.size, optimizer.population * optimizer.generations)
            total_visible = total_expected if total_expected <= 9007199254740991 else None
            self._event('progress', completed=0, total=total_visible, total_expected=str(total_expected), generation=0)
            index = 0
            generation = 0
            with self._worker_pool() as pool:
                while not self.cancel_event.is_set():
                    if optimizer is not None:
                        batch = optimizer.ask()
                    else:
                        batch = []
                        for _ in range(self.concurrency):
                            try:
                                batch.append(next(sweep))
                            except StopIteration:
                                break
                    if not batch:
                        break
                    pending = []
                    for values in batch:
                        index += 1
                        with _NATIVE_LOCK:
                            row, job, started = self._prepare(values, index, generation)
                        if job is None:
                            row['elapsed_seconds'] = round(time.monotonic() - started, 3)
                            pending.append((row, None))
                        else:
                            pending.append((row, pool.submit(self._solve_guarded, row, job, started)))
                        if self.cancel_event.is_set():
                            break
                    batch_rows = []
                    for row, future in pending:
                        if future is not None:
                            row = future.result()
                        # Persist the SNP contract and retry handle before native deletion.
                        _atomic(Path(self.output_dir) / (row['id'] + '-candidate.json'), row)
                        retention = row.get('retention', {})
                        if retention.get('state') == 'pending':
                            try:
                                with _NATIVE_LOCK:
                                    retention['cleanup_log'] = cell_lifecycle.delete(retention['handle'], self.source['runtime'])
                                retention['state'] = 'deleted'
                            except Exception as exc:
                                retention['state'] = 'cleanup_failed'
                                retention['error'] = str(exc)
                                row['warnings'].append('Cell cleanup failed; retry handle retained: ' + str(exc))
                        self._record_failure(row)
                        self.rows.append(row)
                        batch_rows.append(row)
                        _atomic(Path(self.output_dir) / (row['id'] + '-candidate.json'), row)
                        self._save()
                        self._event('candidate', row=copy.deepcopy(row))
                        self._event('progress', completed=len(self.rows), total=total_visible, total_expected=str(total_expected), generation=generation)
                    if optimizer is not None and not self.cancel_event.is_set():
                        optimizer.tell([dict(values=r['values'], objectives=r['objectives'], violation=r['violation'] if r['violation'] is not None else 0.0, ok=r['status'] == 'completed') for r in batch_rows])
                        _atomic(Path(self.output_dir) / 'optimizer-checkpoint.json', dict(algorithm=optimizer.algorithm, generation=optimizer.generation, seen=sorted(optimizer.seen), history=optimizer.history, parents=optimizer.parents, random_state=optimizer.rng.getstate(), objective_count=optimizer.objective_count))
                    generation += 1
            status = ('cancelled' if self.cancel_event.is_set() else
                      'failed' if not any(r['status'] == 'completed' for r in self.rows) else
                      'partial' if any(r['status'] != 'completed' for r in self.rows) else 'completed')
            successful = [dict(r, ok=True) for r in self.rows if r['status'] == 'completed']
            frontier = emx_search.pareto(successful) if self.config.get('objectives') else []
            counts = _counts(self.rows)
            _atomic(Path(self.output_dir) / 'completion.json', dict(status=status, pareto_ids=[r['id'] for r in frontier], candidates=len(self.rows), counts=counts))
            self._event('complete', status=status, output_dir=self.output_dir, rows=copy.deepcopy(self.rows), pareto_ids=[r['id'] for r in frontier], counts=counts)
            return self.rows
        except Exception as exc:
            log_path = self._global_error_log(str(exc))
            message = str(exc) if log_path else str(exc) + ' (Unable to save an error log.)'
            self._event('error', message=message, log_path=log_path)
            self._event('complete', status='failed', output_dir=self.output_dir, rows=copy.deepcopy(self.rows), pareto_ids=[], counts=_counts(self.rows))
            return self.rows
        finally:
            self._active = False
