"""BMP manifest compilation and execution pipeline.

The public names are loaded lazily.  Benchmark adapters import the lightweight
``runner.evidence`` helpers; eagerly importing every backend here creates an
otherwise order-dependent cycle through the AOSE adapter.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_MODULES = {
    "AdapterRegistry": ".adapter_registry",
    "AdapterRegistryError": ".adapter_registry",
    "CompilationError": ".compiler",
    "CompiledRun": ".compiler",
    "Compiler": ".compiler",
    "IsolationViolation": ".compiler",
    "RegistryLookupError": ".compiler",
    "canonical_manifest_json": ".compiler",
    "enforce_allowed_diff": ".compiler",
    "expand_factor_sweep": ".compiler",
    "manifest_sha256": ".compiler",
    "ConfigurationRegistry": ".configuration",
    "ConfigurationRegistryError": ".configuration",
    "ConfigurationDriftError": ".configuration",
}


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError as exc:  # pragma: no cover - normal Python attribute behavior
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "AdapterRegistry",
    "AdapterRegistryError",
    "CompilationError",
    "CompiledRun",
    "Compiler",
    "IsolationViolation",
    "RegistryLookupError",
    "canonical_manifest_json",
    "enforce_allowed_diff",
    "expand_factor_sweep",
    "manifest_sha256",
    "ConfigurationRegistry",
    "ConfigurationRegistryError",
    "ConfigurationDriftError",
]
