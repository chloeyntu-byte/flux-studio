"""Explicit passive-family plugin contracts, independent of external fitters."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field

@dataclass(frozen=True)
class Candidate:
    id: str
    label: str
    sections: int = 1
    parasitic: int = 0
    skin: bool = False
    estimated_elements: int = 0
    bridges: bool = False

@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    device: str
    mode: str
    port_roles: tuple[str, ...]
    explicit_reference: bool
    candidates: tuple[Candidate, ...]
    winding_pairs: tuple[tuple[str, str], ...]
    tap_roles: tuple[str, ...] = field(default_factory=tuple)
    plugin: str = ""
    @property
    def num_ports(self): return len(self.port_roles)
    @property
    def model_id(self): return self.id
    @property
    def nports(self): return self.num_ports
    def as_dict(self):
        out=asdict(self); out['num_ports']=self.num_ports
        return out
