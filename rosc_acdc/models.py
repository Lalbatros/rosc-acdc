"""Model specifications: the load flow / security analysis variants the study compares.

A "model" is one way of solving the network - AC, DC, DC with a different provider option,
... - described by data rather than by code. Every downstream stage iterates over
``config.MODELS``, so comparing another variant is a configuration change.

This module deliberately imports nothing from :mod:`rosc_acdc.config`, so that config.py
can build its ``MODELS`` default from :class:`ModelSpec`.
"""

import logging
from dataclasses import dataclass, field

import pypowsybl as pp

logger = logging.getLogger(__name__)

PROVIDER = "OpenLoadFlow"


@dataclass(frozen=True)
class ModelSpec:
    """One load flow / security analysis variant.

    Attributes:
        name: unique label, used as-is in column names, KPI blocks and plot legends.
        dc: True runs a DC load flow and a DC security analysis, False the AC ones.
        parameters: ``pp.loadflow.Parameters`` keyword arguments, e.g.
            ``{"distributed_slack": False}``. ``dc`` is not accepted here - it comes from
            the field above, because run_ac/run_dc force it anyway.
        provider_parameters: OpenLoadFlow *load flow* options (str -> str), merged onto the
            provider defaults, e.g. ``{"maxNewtonRaphsonIterations": "30"}``.
        sa_provider_parameters: OpenLoadFlow *security analysis* options (str -> str), e.g.
            ``{"dcFastMode": "true"}``. These are a separate namespace from the load flow
            ones; a key put in the wrong bucket is rejected by :func:`validate_models`.
    """

    name: str
    dc: bool
    parameters: dict = field(default_factory=dict)
    provider_parameters: dict = field(default_factory=dict)
    sa_provider_parameters: dict = field(default_factory=dict)


def build_lf_parameters(spec: ModelSpec) -> pp.loadflow.Parameters:
    """The ``pp.loadflow.Parameters`` for `spec`.

    ``pp.loadflow.Parameters()`` already carries every OpenLoadFlow default, so the spec's
    provider parameters are merged onto them instead of replacing them.
    """
    parameters = pp.loadflow.Parameters(**spec.parameters)
    if spec.provider_parameters:
        parameters.provider_parameters = {
            **parameters.provider_parameters,
            **spec.provider_parameters,
        }
    # run_ac / run_dc force this too; setting it keeps the object self-describing in logs.
    parameters.dc = spec.dc
    return parameters


def build_sa_parameters(spec: ModelSpec) -> pp.security.Parameters:
    """The ``pp.security.Parameters`` for `spec`.

    Starts from ``pp.security.Parameters()`` - the very object a security analysis uses when
    called without parameters - and applies only what the spec overrides, so a spec with no
    overrides runs on untouched defaults. Note that its default provider parameters (both the
    security-analysis ones and the nested load flow ones) are empty dicts, unlike
    ``pp.loadflow.Parameters()``, which is why they are merged rather than assigned.
    """
    parameters = pp.security.Parameters()
    for name, value in spec.parameters.items():
        setattr(parameters.load_flow_parameters, name, value)
    if spec.provider_parameters:
        parameters.load_flow_parameters.provider_parameters = {
            **parameters.load_flow_parameters.provider_parameters,
            **spec.provider_parameters,
        }
    if spec.sa_provider_parameters:
        parameters.provider_parameters = {
            **parameters.provider_parameters,
            **spec.sa_provider_parameters,
        }
    return parameters


def validate_models(model_specs, reference: str) -> None:
    """Fail fast on an unusable ``config.MODELS`` / ``config.REFERENCE_MODEL``.

    pypowsybl silently ignores unknown provider parameter keys, so a typo would otherwise
    produce a model that is a duplicate of another one under a different name. Everything
    that can be checked without the network is checked here, before it is loaded.
    """
    if not model_specs:
        raise ValueError("config.MODELS is empty: at least one model is required")

    names = [spec.name for spec in model_specs]
    for spec in model_specs:
        if not isinstance(spec.name, str) or not spec.name:
            raise ValueError(f"model names must be non-empty strings, got {spec.name!r}")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate model names in config.MODELS: {', '.join(duplicates)}")
    if reference not in names:
        raise ValueError(
            f"config.REFERENCE_MODEL {reference!r} is not one of the model names: "
            f"{', '.join(names)}"
        )

    lf_keys = set(pp.loadflow.get_provider_parameters(PROVIDER).index)
    sa_keys = set(pp.security.get_provider_parameters_names(PROVIDER))

    for spec in model_specs:
        if "dc" in spec.parameters:
            raise ValueError(
                f"model {spec.name!r}: 'dc' must not be set in parameters; "
                "use ModelSpec(..., dc=True/False) instead"
            )
        try:
            pp.loadflow.Parameters(**spec.parameters)
        except Exception as error:  # TypeError for a bad kwarg, PyPowsyblError for a bad combination
            raise ValueError(f"model {spec.name!r}: invalid parameters: {error}") from error

        _check_provider_parameters(spec.name, spec.provider_parameters, "provider_parameters",
                                   lf_keys, "sa_provider_parameters", sa_keys)
        _check_provider_parameters(spec.name, spec.sa_provider_parameters, "sa_provider_parameters",
                                  sa_keys, "provider_parameters", lf_keys)


def _check_provider_parameters(model_name, values, field_name, valid_keys, other_field, other_keys):
    """Reject unknown provider parameter keys, pointing at the other bucket when that is the mistake."""
    for key, value in values.items():
        if key not in valid_keys:
            hint = f" It is a {other_field} key." if key in other_keys else ""
            raise ValueError(
                f"model {model_name!r}: unknown {PROVIDER} key {key!r} in {field_name}.{hint} "
                "pypowsybl ignores unknown keys silently, so they are rejected here."
            )
        if not isinstance(value, str):
            raise ValueError(
                f"model {model_name!r}: {field_name}[{key!r}] must be a string, "
                f"got {value!r} ({type(value).__name__})"
            )
