"""Explicit selection registry; no generic structure guessing/fitting fallback."""
from .base import Candidate, ModelSpec
from .inductor import model_specs as inductor_specs
from .transformer import model_specs as transformer_specs
from .inductor import add_loss_candidate as inductor_loss
from .transformer import add_loss_candidate as transformer_loss

_MODELS = {}
_FITTERS = {}


def register_model(spec, fitter=None):
    if spec.id in _MODELS:
        raise ValueError("Duplicate model plugin ID: " + spec.id)
    _MODELS[spec.id] = spec
    if fitter is not None:
        if not callable(fitter):
            del _MODELS[spec.id]
            raise TypeError("A model fitter must be callable")
        _FITTERS[spec.id] = fitter


for _spec in inductor_specs() + transformer_specs():
    register_model(_spec)


def list_models():
    return list(_MODELS.values())


def get_model(model_id):
    try:
        return _MODELS[model_id]
    except KeyError:
        raise ValueError("Select a supported explicit model: %s" % model_id) from None


def add_loss_candidate(circuit, network, spec, metrics, cell_index):
    adapters = {"inductor": inductor_loss, "transformer": transformer_loss}
    return adapters[spec.plugin](circuit, network, spec, metrics, cell_index)


def get_fitter(model_id):
    get_model(model_id)
    return _FITTERS.get(model_id)
