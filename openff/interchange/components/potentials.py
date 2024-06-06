"""Models for storing applied force field parameters."""

import ast
import json
import warnings
from typing import Annotated, Any, Union

import numpy
from openff.models.models import DefaultModel
from openff.toolkit import Quantity
from openff.utilities.utilities import has_package, requires_package
from pydantic import (
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    WrapSerializer,
)
from pydantic.functional_validators import WrapValidator

from openff.interchange._annotations import _Quantity
from openff.interchange._pydantic import Field, PrivateAttr
from openff.interchange.exceptions import MissingParametersError
from openff.interchange.models import (
    LibraryChargeTopologyKey,
    PotentialKey,
    TopologyKey,
)
from openff.interchange.warnings import InterchangeDeprecationWarning

if has_package("jax"):
    from jax import numpy as jax_numpy

from numpy.typing import ArrayLike

if has_package("jax"):
    from jax import Array


def __getattr__(name: str):
    if name == "PotentialHandler":
        warnings.warn(
            "`PotentialHandler` has been renamed to `Collection`. "
            "Importing `Collection` instead.",
            InterchangeDeprecationWarning,
            stacklevel=2,
        )
        return Collection

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def potential_loader(data: str) -> dict:
    """Load a JSON blob dumped from a `Collection`."""
    tmp: dict[str, int | bool | str | dict] = {}

    for key, val in json.loads(data).items():
        if isinstance(val, (str, type(None))):
            tmp[key] = val  # type: ignore
        elif isinstance(val, dict):
            if key == "parameters":
                tmp["parameters"] = dict()

                for key_, val_ in val.items():
                    loaded = json.loads(val_)
                    tmp["parameters"][key_] = Quantity(  # type: ignore[index]
                        loaded["val"],
                        loaded["unit"],
                    )

    return tmp


def validate_parameters(
    v: Any,
    handler: ValidatorFunctionWrapHandler,
    info: ValidationInfo,
) -> dict[str, Quantity]:
    """Validate the parameters field of a Potential object."""
    if info.mode in ("json", "python"):
        tmp: dict[str, int | bool | str | dict] = {}

        for key, val in v.items():
            if isinstance(val, dict):
                print(f"turning {val} of type {type(val)} into a quantity ...")
                quantity_dict = json.loads(val)
                tmp[key] = Quantity(
                    quantity_dict["val"],
                    quantity_dict["unit"],
                )
            elif isinstance(val, Quantity):
                tmp[key] = val
            elif isinstance(val, str):
                loaded = json.loads(val)
                if isinstance(loaded, dict):
                    tmp[key] = Quantity(
                        loaded["val"],
                        loaded["unit"],
                    )
                else:
                    tmp[key] = val

            else:
                raise ValidationError(
                    f"Unexpected type {type(val)} found in JSON blob.",
                )

        return tmp


def serialize_parameters(value: dict[str, Quantity], handler, info) -> dict[str, str]:
    """Serialize the parameters field of a Potential object."""
    if info.mode == "json":
        return {
            k: json.dumps(
                {
                    "val": v.m,
                    "unit": str(v.units),
                },
            )
            for k, v in value.items()
        }


ParameterDict = Annotated[
    dict[str, Any],
    WrapValidator(validate_parameters),
    WrapSerializer(serialize_parameters),
]


class Potential(DefaultModel):
    """Base class for storing applied parameters."""

    parameters: dict[str, _Quantity] = Field(dict())
    map_key: int | None = None

    def __hash__(self) -> int:
        return hash(tuple(self.parameters.values()))


class WrappedPotential(DefaultModel):
    """Model storing other Potential model(s) inside inner data."""

    _inner_data: dict[Potential, float] = PrivateAttr()

    def __init__(self, data: Potential | dict) -> None:
        # Needed to set some Pydantic magic, at least __pydantic_private__;
        # won't actually process the input here
        super().__init__()

        if isinstance(data, Potential):
            data = {data: 1.0}

        self._inner_data = data

    @property
    def parameters(self) -> dict[str, Quantity]:
        """Get the parameters as represented by the stored potentials and coefficients."""
        keys: set[str] = {
            param_key
            for pot in self._inner_data.keys()
            for param_key in pot.parameters.keys()
        }
        params = dict()
        for key in keys:
            params.update(
                {
                    key: sum(
                        coeff * pot.parameters[key]
                        for pot, coeff in self._inner_data.items()
                    ),
                },
            )
        return params

    def __repr__(self) -> str:
        return str(self._inner_data)


def validate_potential_or_wrapped_potential(
    v: Any,
    handler: ValidatorFunctionWrapHandler,
    info: ValidationInfo,
) -> dict[str, Quantity]:
    """Validate the parameters field of a Potential object."""
    if info.mode == "json":
        if "parameters" in v:
            return Potential.model_validate(v)
        else:
            return WrappedPotential.model_validate(v)


PotentialOrWrappedPotential = Annotated[
    Union[Potential, WrappedPotential],
    WrapValidator(validate_potential_or_wrapped_potential),
]


def validate_key_map(v: Any, handler, info) -> dict:
    """Validate the key_map field of a Collection object."""
    from openff.interchange.models import (
        AngleKey,
        BondKey,
        ImproperTorsionKey,
        LibraryChargeTopologyKey,
        ProperTorsionKey,
        SingleAtomChargeTopologyKey,
    )

    tmp = dict()
    if info.mode in ("json", "python"):
        for key, val in v.items():
            val_dict = json.loads(val)

            match val_dict["associated_handler"]:
                case "Bonds":
                    key_class = BondKey
                case "Angles":
                    key_class = AngleKey
                case "ProperTorsions":
                    key_class = ProperTorsionKey
                case "ImproperTorsions":
                    key_class = ImproperTorsionKey
                case "LibraryCharges":
                    key_class = LibraryChargeTopologyKey
                case "ToolkitAM1BCCHandler":
                    key_class = SingleAtomChargeTopologyKey

                case _:
                    key_class = TopologyKey

            try:
                tmp.update(
                    {
                        key_class.model_validate_json(
                            key,
                        ): PotentialKey.model_validate_json(val),
                    },
                )
            except Exception:
                raise ValueError(val_dict["associated_handler"])

            del key_class

        v = tmp

    else:
        raise ValueError(f"Validation mode {info.mode} not implemented.")

    return v


def serialize_key_map(value: dict[str, str], handler, info) -> dict[str, str]:
    """Serialize the parameters field of a Potential object."""
    if info.mode == "json":
        return {
            key.model_dump_json(): value.model_dump_json()
            for key, value in value.items()
        }

    else:
        raise NotImplementedError(f"Serialization mode {info.mode} not implemented.")


KeyMap = Annotated[
    dict[TopologyKey | LibraryChargeTopologyKey, PotentialKey],
    WrapValidator(validate_key_map),
    WrapSerializer(serialize_key_map),
]


def validate_potential_dict(
    v: Any,
    handler: ValidatorFunctionWrapHandler,
    info: ValidationInfo,
):
    """Validate the parameters field of a Potential object."""
    if info.mode == "json":
        return {
            PotentialKey.model_validate_json(key): Potential.model_validate_json(val)
            for key, val in v.items()
        }

    return v


def serialize_potential_dict(
    value: dict[str, Quantity],
    handler,
    info,
) -> dict[str, str]:
    """Serialize the parameters field of a Potential object."""
    if info.mode == "json":
        return {
            key.model_dump_json(): value.model_dump_json()
            for key, value in value.items()
        }


Potentials = Annotated[
    dict[PotentialKey, PotentialOrWrappedPotential],
    WrapValidator(validate_potential_dict),
    WrapSerializer(serialize_potential_dict),
]


class Collection(DefaultModel):
    """Base class for storing parametrized force field data."""

    type: str = Field(..., description="The type of potentials this handler stores.")
    is_plugin: bool = Field(
        False,
        description="Whether this collection is defined as a plugin.",
    )
    expression: str = Field(
        ...,
        description="The analytical expression governing the potentials in this handler.",
    )
    key_map: KeyMap = Field(
        dict(),
        description="A mapping between TopologyKey objects and PotentialKey objects.",
    )
    potentials: Potentials = Field(
        dict(),
        description="A mapping between PotentialKey objects and Potential objects.",
    )

    @property
    def independent_variables(self) -> set[str]:
        """
        Return a set of variables found in the expression but not in any potentials.
        """
        vars_in_potentials = set([*self.potentials.values()][0].parameters.keys())
        vars_in_expression = {
            node.id
            for node in ast.walk(ast.parse(self.expression))
            if isinstance(node, ast.Name)
        }
        return vars_in_expression - vars_in_potentials

    def _get_parameters(self, atom_indices: tuple[int]) -> dict:
        for topology_key in self.key_map:
            if topology_key.atom_indices == atom_indices:
                potential_key = self.key_map[topology_key]
                potential = self.potentials[potential_key]
                parameters = potential.parameters
                return parameters
        raise MissingParametersError(
            f"Could not find parameter in parameter in handler {self.type} "
            f"associated with atoms {atom_indices}",
        )

    def get_force_field_parameters(
        self,
        use_jax: bool = False,
    ) -> Union["ArrayLike", "Array"]:
        """Return a flattened representation of the force field parameters."""
        # TODO: Handle WrappedPotential
        if any(
            isinstance(potential, WrappedPotential)
            for potential in self.potentials.values()
        ):
            raise NotImplementedError

        if use_jax:
            return jax_numpy.array(
                [
                    [v.m for v in p.parameters.values()]
                    for p in self.potentials.values()
                ],
            )
        else:
            return numpy.array(
                [
                    [v.m for v in p.parameters.values()]
                    for p in self.potentials.values()
                ],
            )

    def set_force_field_parameters(self, new_p: "ArrayLike") -> None:
        """Set the force field parameters from a flattened representation."""
        mapping = self.get_mapping()
        if new_p.shape[0] != len(mapping):  # type: ignore
            raise RuntimeError

        for potential_key, potential_index in self.get_mapping().items():
            potential = self.potentials[potential_key]
            if len(new_p[potential_index, :]) != len(potential.parameters):  # type: ignore
                raise RuntimeError

            for parameter_index, parameter_key in enumerate(potential.parameters):
                parameter_units = potential.parameters[parameter_key].units
                modified_parameter = new_p[potential_index, parameter_index]  # type: ignore

                self.potentials[potential_key].parameters[parameter_key] = (
                    modified_parameter * parameter_units
                )

    def get_system_parameters(
        self,
        p=None,
        use_jax: bool = False,
    ) -> Union["ArrayLike", "Array"]:
        """
        Return a flattened representation of system parameters.

        These values are effectively force field parameters as applied to a chemical topology.
        """
        # TODO: Handle WrappedPotential
        if any(
            isinstance(potential, WrappedPotential)
            for potential in self.potentials.values()
        ):
            raise NotImplementedError

        if p is None:
            p = self.get_force_field_parameters(use_jax=use_jax)
        mapping = self.get_mapping()

        q: list = list()
        for potential_key in self.key_map.values():
            index = mapping[potential_key]
            q.append(p[index])

        if use_jax:
            return jax_numpy.array(q)
        else:
            return numpy.array(q)

    def get_mapping(self) -> dict[PotentialKey, int]:
        """Get a mapping between potentials and array indices."""
        mapping: dict = dict()
        index = 0
        for potential_key in self.key_map.values():
            if potential_key not in mapping:
                mapping[potential_key] = index
                index += 1

        return mapping

    def parametrize(
        self,
        p=None,
        use_jax: bool = True,
    ) -> Union["ArrayLike", "Array"]:
        """Return an array of system parameters, given an array of force field parameters."""
        if p is None:
            p = self.get_force_field_parameters(use_jax=use_jax)

        return self.get_system_parameters(p=p, use_jax=use_jax)

    def parametrize_partial(self):
        """Return a function that will call `self.parametrize()` with arguments specified by `self.mapping`."""
        from functools import partial

        return partial(
            self.parametrize,
            mapping=self.get_mapping(),
        )

    @requires_package("jax")
    def get_param_matrix(self) -> Union["Array", "ArrayLike"]:
        """Get a matrix representing the mapping between force field and system parameters."""
        from functools import partial

        import jax

        p = self.get_force_field_parameters(use_jax=True)

        parametrize_partial = partial(
            self.parametrize,
        )

        jac_parametrize = jax.jacfwd(parametrize_partial)
        jac_res = jac_parametrize(p)

        return jac_res.reshape(-1, p.flatten().shape[0])  # type: ignore[union-attr]

    def __getattr__(self, attr: str):
        if attr == "slot_map":
            warnings.warn(
                "The `slot_map` attribute is deprecated. Use `key_map` instead.",
                InterchangeDeprecationWarning,
                stacklevel=2,
            )
            return self.key_map
        else:
            return super().__getattribute__(attr)


def validate_collections(
    v: Any,
    handler: ValidatorFunctionWrapHandler,
    info: ValidationInfo,
) -> dict:
    """Validate the collections dict from a JSON blob."""
    from openff.interchange.smirnoff import (
        SMIRNOFFAngleCollection,
        SMIRNOFFBondCollection,
        SMIRNOFFConstraintCollection,
        SMIRNOFFElectrostaticsCollection,
        SMIRNOFFImproperTorsionCollection,
        SMIRNOFFProperTorsionCollection,
        SMIRNOFFvdWCollection,
        SMIRNOFFVirtualSiteCollection,
    )

    _class_mapping = {
        "Bonds": SMIRNOFFBondCollection,
        "Angles": SMIRNOFFAngleCollection,
        "Constraints": SMIRNOFFConstraintCollection,
        "ProperTorsions": SMIRNOFFProperTorsionCollection,
        "ImproperTorsions": SMIRNOFFImproperTorsionCollection,
        "vdW": SMIRNOFFvdWCollection,
        "Electrostatics": SMIRNOFFElectrostaticsCollection,
        "VirtualSites": SMIRNOFFVirtualSiteCollection,
    }

    if info.mode in ("json", "python"):
        return {
            collection_name: _class_mapping[collection_name].model_validate(
                collection_data,
            )
            for collection_name, collection_data in v.items()
        }


_AnnotatedCollections = Annotated[
    dict[str, Collection],
    WrapValidator(validate_collections),
]
