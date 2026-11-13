"""Automatic, bounded L-first parameter refinement of one passive topology.

This module adds no devices and changes no acceptance threshold. Q, complete
complex S and transformer mutual impedance remain independent constraints.
"""
from __future__ import annotations

import copy
import time
import numpy as np


def select_strategy(l_pct, q_pct):
    """A smaller permitted L error requests higher L precision automatically."""
    return 'l_priority' if float(l_pct) < float(q_pct) else 'balanced'


def l_score(metrics):
    return max(float(branch['l']['score']) for branch in metrics['branches'].values())


def guard_score(metrics):
    values = [float(branch['q']['score']) for branch in metrics['branches'].values()]
    network = metrics['network']
    values.append(float(network['max_s_absolute_error']) / float(network['limit_s_absolute_error']))
    values += [float(item['score']) for item in
               metrics.get('mutual_impedance', {}).get('complex_relative_errors', {}).values()]
    return max(values)


def preference(metrics):
    """Never trade an accepted result for one that violates any hard limit."""
    guard = guard_score(metrics)
    winding = l_score(metrics)
    return (not bool(metrics['passed']), guard > 1,
            guard if guard > 1 else winding, winding if guard > 1 else guard)


def polish(circuit, network, spec, limits, timeout_s, cancel=None, progress=None):
    """Spend one finite budget reducing worst L error under Q/S/mutual guards.

    Two fixed internal targets improve the existing values; they are not new
    user error limits. Every saved candidate is checked with the independent
    circuit evaluator. An accepted input is retained if refinement is rejected.
    """
    from .optimizer import SensitivityNetwork
    from .circuit import simulate
    from .fitting import evaluate, _measure_s

    started = time.monotonic()
    original = copy.deepcopy(circuit)
    original_metrics = evaluate(original, network, spec, limits)
    best = original
    best_metrics = original_metrics
    iterations = 0
    accepted_steps = 0
    factors = (.6, .2)
    receipt = {
        'algorithm': 'bounded_minimax_l_with_q_network_mutual_guards',
        'strategy': select_strategy(limits['l_pct'], limits['q_pct']),
        'max_iterations': 160, 'internal_target_factors': list(factors),
        'initial_l_score': l_score(original_metrics),
        'initial_guard_score': guard_score(original_metrics),
        'added_elements': 0, 'external_tools': [], 'timeout_s': float(timeout_s),
        'internal_guard_safety_ratio': .999,
    }

    def cancelled():
        return cancel is not None and cancel.is_set()

    def stopped():
        return cancelled() or time.monotonic() - started >= max(0, float(timeout_s))

    def finish(state):
        nonlocal best_metrics
        if cancelled():
            state = 'cancelled'
            best_metrics = copy.deepcopy(best_metrics)
            best_metrics['passed'] = False
        receipt.update(state=state, iterations=iterations, accepted_steps=accepted_steps,
                       elapsed_s=time.monotonic() - started,
                       final_l_score=l_score(best_metrics),
                       final_guard_score=guard_score(best_metrics))
        return best, best_metrics, receipt

    if cancelled():
        return finish('cancelled')
    if receipt['strategy'] != 'l_priority':
        return finish('not_requested')
    if stopped():
        return finish('budget_exhausted')
    if l_score(original_metrics) <= 1e-7:
        return finish('already_exact')
    if progress:
        progress('自动 L 优先：同一结构内压低 L 最差误差，保持 Q/S/互阻抗门限')

    source, source_z = _measure_s(network.s, network.z0, network.freq, spec)
    nf = len(network.freq)
    qscale = float(limits['q_pct']) / 100
    lscale = float(limits['l_pct']) / 100
    sscale = float(limits.get('network_pct', min(limits['l_pct'], limits['q_pct']))) / 100
    mscale = min(lscale, qscale)
    lden, qden = {}, {}
    for name, curves in source.items():
        for key, destination in [('l_h', lden), ('q', qden)]:
            values = curves[key]
            destination[name] = np.maximum(np.abs(values),
                max(float(np.max(np.abs(values))) * 1e-6, np.finfo(float).tiny))

    def blocks(s_batch):
        sf = np.asarray(s_batch)
        count = sf.shape[0]
        curves, z = _measure_s(sf.reshape(count * nf, network.nports, network.nports),
                              network.z0, np.tile(network.freq, count), spec)
        lparts, qparts, complex_guards = [], [], []
        for name in source:
            lparts.append((curves[name]['l_h'].reshape(count, nf) -
                           source[name]['l_h'][None]) / lden[name][None] / lscale)
            qparts.append((curves[name]['q'].reshape(count, nf) -
                           source[name]['q'][None]) / qden[name][None] / qscale)
        complex_guards.append((sf - network.s[None]).reshape(count, -1) / sscale)
        if source_z.shape[-1] == 2:
            z = z.reshape(count, nf, 2, 2)
            for a, b in ((0, 1), (1, 0)):
                reference = source_z[:, a, b]
                floor = max(float(np.max(np.abs(reference))) * 1e-6, np.finfo(float).tiny)
                denom = np.maximum(np.abs(reference), floor) * mscale
                complex_guards.append((z[:, :, a, b] - reference[None]) / denom[None])
        dl = np.concatenate(lparts, axis=1)
        dq = np.concatenate(qparts, axis=1)
        dg = np.concatenate(complex_guards, axis=1)
        guards = np.maximum(np.max(np.abs(dq), axis=1), np.max(np.abs(dg), axis=1))
        if not all(np.all(np.isfinite(v)) for v in (dl, dq, dg, guards)):
            raise ValueError('Nonfinite L-priority residual')
        return dl, dq, dg, guards

    def residual(s_batch, factor):
        dl, dq, dg, _ = blocks(s_batch)
        qpen = np.sign(dq) * np.maximum(np.abs(dq) - .97, 0) * 50
        magnitude = np.abs(dg)
        cpen = dg / np.maximum(magnitude, 1e-30) * np.maximum(magnitude - .97, 0) * 50
        return np.concatenate((dl / factor, qpen, cpen.real, cpen.imag), axis=1)

    def fast_key(sf):
        dl, _, _, guard = blocks(sf[None])
        peak = float(np.max(np.abs(dl)))
        value = float(guard[0])
        # A small internal margin improves native replay robustness. The user
        # limits and independent acceptance evaluator remain exactly unchanged.
        return (value > .999, value if value > .999 else peak), value

    best_fast, _ = fast_key(simulate(best, network.freq, network.z0))
    stages = []
    try:
        for factor in factors:
            if stopped():
                break
            graph = SensitivityNetwork(best, network.freq)
            x = np.zeros(graph.ng)
            damping = 1e-2
            stage_iterations = 0
            for _ in range(80):
                if stopped():
                    break
                iterations += 1
                stage_iterations += 1
                sf, ds = graph.s(x, network.z0, True)
                r = residual(sf[None], factor)[0]
                eps = 1e-5
                jac = ((residual(sf[None] + eps * ds, factor) - r[None]) / eps).T
                weights = np.ones(len(r))
                lcount = nf * len(source)
                peak = max(float(np.max(np.abs(r[:lcount]))), 1e-12)
                weights[:lcount] = np.maximum(.08, np.abs(r[:lcount]) / peak) ** 4
                normal = jac.T @ (weights[:, None] * jac)
                gradient = jac.T @ (weights * r)
                diagonal = np.maximum(np.diag(normal), 1e-12)
                old_cost = float(np.dot(weights * r, r))
                _, old_guard = fast_key(sf)
                accepted = False
                for _attempt in range(6):
                    if stopped():
                        break
                    try:
                        step = -np.linalg.solve(normal + damping * np.diag(diagonal), gradient)
                    except np.linalg.LinAlgError:
                        damping *= 5
                        continue
                    step = np.clip(step, -.35, .35)
                    for backtrack in range(5):
                        if stopped():
                            break
                        trial = x + step * (.5 ** backtrack)
                        try:
                            ss = graph.s(trial, network.z0)
                            key, new_guard = fast_key(ss)
                            if old_guard <= .999 and new_guard > .999:
                                continue
                            nr = residual(ss[None], factor)[0]
                            cost = float(np.dot(weights * nr, nr))
                            if cost >= old_cost:
                                continue
                            x = trial
                            accepted = True
                            accepted_steps += 1
                            damping = max(1e-9, damping / 3)
                            if key < best_fast:
                                cc = graph.circuit(x)
                                mm = evaluate(cc, network, spec, limits)
                                # A newly selected feasible point keeps 0.1%
                                # guard headroom for native replay. The input
                                # remains available if no safe point exists.
                                safe = not mm['passed'] or guard_score(mm) <= .999
                                if safe and preference(mm) < preference(best_metrics):
                                    best, best_metrics = cc, mm
                                    best_fast = key
                            break
                        except (ValueError, np.linalg.LinAlgError, FloatingPointError):
                            continue
                    if accepted:
                        break
                    damping *= 5
                if not accepted:
                    break
            stages.append({'target_factor': factor, 'iterations': stage_iterations,
                           'best_l_score': l_score(best_metrics),
                           'best_guard_score': guard_score(best_metrics)})
    except (ValueError, np.linalg.LinAlgError, FloatingPointError) as error:
        receipt['numerical_error'] = str(error)
    receipt['stages'] = stages
    return finish('budget_exhausted' if stopped() and not cancelled() else 'completed')
