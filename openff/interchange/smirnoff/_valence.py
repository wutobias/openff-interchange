import warnings
from typing import Dict, List, Literal, Type

from openff.toolkit import Molecule, Topology
from openff.toolkit.typing.engines.smirnoff.parameters import (
    AngleHandler,
    BondHandler,
    ConstraintHandler,
    ImproperTorsionHandler,
    ParameterHandler,
    ProperTorsionHandler,
)
from openff.units import unit

from openff.interchange.components.potentials import Potential, WrappedPotential
from openff.interchange.exceptions import (
    InvalidParameterHandlerError,
    MissingParametersError,
)
from openff.interchange.models import (
    BondKey,
    ImproperTorsionKey,
    PotentialKey,
    ProperTorsionKey,
)
from openff.interchange.smirnoff._base import (
    SMIRNOFFCollection,
    T,
    _check_all_valence_terms_assigned,
)


def _upconvert_bondhandler(bond_handler: BondHandler):
    """Given a BondHandler with version 0.3, up-convert to 0.4."""
    from packaging.version import Version

    if bond_handler.version >= Version("0.4"):
        return

    elif bond_handler.version == Version("0.3"):
        warnings.warn(
            "Automatically up-converting BondHandler from version 0.3 to 0.4. Consider manually upgrading "
            "this BondHandler (or <Bonds> section in an OFFXML file) to 0.4 or newer. For more details, "
            "see https://openforcefield.github.io/standards/standards/smirnoff/#bonds.",
        )

        bond_handler.version = Version("0.4")
        bond_handler.potential = "(k/2)*(r-length)^2"


def _check_partial_bond_orders(
    reference_molecule: Molecule,
    molecule_list: List[Molecule],
) -> bool:
    """Check if the reference molecule is isomorphic with any molecules in a provided list."""
    if molecule_list is None:
        return False

    if len(molecule_list) == 0:
        return False

    for molecule in molecule_list:
        if reference_molecule.is_isomorphic_with(molecule):
            # TODO: Here is where a check for "all bonds in this molecule must have partial bond orders assigned"
            #       would go. That seems like a difficult mangled state to end up in, so not implemented for now.
            return True

    return False


def _get_interpolation_coeffs(fractional_bond_order, data):
    x1, x2 = data.keys()
    coeff1 = (x2 - fractional_bond_order) / (x2 - x1)
    coeff2 = (fractional_bond_order - x1) / (x2 - x1)

    return coeff1, coeff2


class SMIRNOFFBondCollection(SMIRNOFFCollection):
    """Collection storing bond potentials as produced by a SMIRNOFF force field."""

    type: Literal["Bonds"] = "Bonds"
    expression: Literal["k/2*(r-length)**2"] = "k/2*(r-length)**2"
    fractional_bond_order_method: Literal["AM1-Wiberg", "None", "none"] = "AM1-Wiberg"
    fractional_bond_order_interpolation: Literal["linear"] = "linear"

    @classmethod
    def allowed_parameter_handlers(cls):
        """Return a list of allowed types of ParameterHandler classes."""
        return [BondHandler]

    @classmethod
    def supported_parameters(cls):
        """Return a list of supported parameter attribute names."""
        return ["smirks", "id", "k", "length", "k_bondorder", "length_bondorder"]

    @classmethod
    def potential_parameters(cls):
        """Return a list of names of parameters included in each potential in this colletion."""
        return ["k", "length"]

    @classmethod
    def valence_terms(cls, topology):
        """Return all bonds in this topology."""
        return [tuple(b.atoms) for b in topology.bonds]

    def store_matches(
        self,
        parameter_handler: ParameterHandler,
        topology: Topology,
    ) -> None:
        """
        Populate self.key_map with key-val pairs of slots and unique potential identifiers.
        """
        if self.key_map:
            # TODO: Should the key_map always be reset, or should we be able to partially
            # update it? Also Note the duplicated code in the child classes
            self.key_map: Dict[BondKey, PotentialKey] = dict()  # type: ignore[assignment]
        matches = parameter_handler.find_matches(topology)
        for key, val in matches.items():
            param = val.parameter_type
            if param.k_bondorder or param.length_bondorder:
                bond = topology.get_bond_between(*key)
                fractional_bond_order = bond.fractional_bond_order
                if not fractional_bond_order:
                    assert self._get_uses_interpolation(parameter_handler)
                    raise RuntimeError(
                        "Bond orders should already be assigned at this point",
                    )
            else:
                fractional_bond_order = None
            topology_key = BondKey(
                atom_indices=key,
                bond_order=fractional_bond_order,
            )

            potential_key = PotentialKey(
                id=val.parameter_type.smirks,
                associated_handler=parameter_handler.TAGNAME,
                bond_order=fractional_bond_order,
            )
            self.key_map[topology_key] = potential_key

        valence_terms = self.valence_terms(topology)

        _check_all_valence_terms_assigned(
            handler=parameter_handler,
            topology=topology,
            assigned_terms=matches,
            valence_terms=valence_terms,
        )

    def store_potentials(self, parameter_handler: BondHandler) -> None:
        """
        Populate self.potentials with key-val pairs of [BondKey, PotentialKey].

        """
        if self.potentials:
            self.potentials = dict()
        for topology_key, potential_key in self.key_map.items():
            smirks = potential_key.id
            force_field_parameters = parameter_handler.parameters[smirks]

            if topology_key.bond_order:
                bond_order = topology_key.bond_order
                if force_field_parameters.k_bondorder:
                    data = force_field_parameters.k_bondorder
                else:
                    data = force_field_parameters.length_bondorder
                coeffs = _get_interpolation_coeffs(
                    fractional_bond_order=bond_order,
                    data=data,
                )
                pots = []
                map_keys = [*data.keys()]
                for map_key in map_keys:
                    pots.append(
                        potential=Potential(
                            parameters={
                                parameter: getattr(
                                    force_field_parameters,
                                    parameter + "_bondorder",
                                )[map_key]
                                for parameter in self.potential_parameters()
                            },
                            map_key=map_key,
                        ),
                    )

                potential = WrappedPotential(
                    {pot: coeff for pot, coeff in zip(pots, coeffs)},
                )

            else:
                potential = Potential(
                    parameters={
                        parameter: getattr(force_field_parameters, parameter)
                        for parameter in self.potential_parameters()
                    },
                )

            self.potentials[potential_key] = potential

    def _get_uses_interpolation(self, parameter_handler: BondHandler) -> bool:
        for parameter in parameter_handler.parameters:
            if parameter.k_bondorder is not None:
                return True
            if parameter.length_bondorder is not None:
                return True

        return False

    @classmethod
    def create(
        cls: Type[T],
        parameter_handler: BondHandler,
        topology: Topology,
        partial_bond_orders_from_molecules=None,
    ) -> T:
        """
        Create a SMIRNOFFBondCollection from toolkit data.

        """
        # TODO: This method overrides SMIRNOFFCollection.from_toolkit in order to gobble up
        # a ConstraintHandler. This seems like a good solution for the interdependence, but is also
        # not a great practice. A better solution would involve not overriding the method with a
        # different function signature.
        if type(parameter_handler) not in cls.allowed_parameter_handlers():
            raise InvalidParameterHandlerError

        handler: T = cls(
            type="Bonds",
            expression="k/2*(r-length)**2",
            fractional_bond_order_method=parameter_handler.fractional_bondorder_method,
            fractional_bond_order_interpolation=parameter_handler.fractional_bondorder_interpolation,
        )

        if handler._get_uses_interpolation(parameter_handler):
            for molecule in topology.molecules:
                if _check_partial_bond_orders(
                    molecule,
                    partial_bond_orders_from_molecules,
                ):
                    continue
                # TODO: expose conformer generation and fractional bond order assigment
                # knobs to user via API
                molecule.generate_conformers(n_conformers=1)
                molecule.assign_fractional_bond_orders(
                    bond_order_model=handler.fractional_bond_order_method.lower(),
                )

        handler.store_matches(parameter_handler=parameter_handler, topology=topology)
        handler.store_potentials(parameter_handler=parameter_handler)

        return handler


class SMIRNOFFConstraintCollection(SMIRNOFFCollection):
    """Handler storing constraint potentials as produced by a SMIRNOFF force field."""

    type: Literal["Constraints"] = "Constraints"
    expression: Literal[""] = ""
    constraints: Dict[
        PotentialKey,
        bool,
    ] = dict()  # should this be named potentials for consistency?

    @classmethod
    def allowed_parameter_handlers(cls):
        """Return a list of allowed types of ParameterHandler classes."""
        return [BondHandler, ConstraintHandler]

    @classmethod
    def supported_parameters(cls):
        """Return a list of supported parameter attribute names."""
        return ["smirks", "id", "length", "distance"]

    @classmethod
    def potential_parameters(cls):
        """Return a list of names of parameters included in each potential in this colletion."""
        return ["length", "distance"]

    @classmethod
    def create(  # type: ignore[override]
        cls: Type[T],
        parameter_handler: List,
        topology: Topology,
    ) -> T:
        """
        Create a SMIRNOFFCollection from toolkit data.

        """
        if isinstance(parameter_handler, list):
            parameter_handlers = parameter_handler
        else:
            parameter_handlers = [parameter_handler]

        for parameter_handler in parameter_handlers:
            if type(parameter_handler) not in cls.allowed_parameter_handlers():
                raise InvalidParameterHandlerError(type(parameter_handler))

        handler = cls()
        handler.store_constraints(
            parameter_handlers=parameter_handlers,
            topology=topology,
        )

        return handler

    def store_constraints(
        self,
        parameter_handlers: List,
        topology: Topology,
    ) -> None:
        """Store constraints."""
        if self.key_map:
            self.key_map = dict()

        try:
            constraint_handler = [
                p for p in parameter_handlers if type(p) == ConstraintHandler
            ][0]
        except IndexError:
            return

        constraint_matches = constraint_handler.find_matches(topology)

        if any([type(p) == BondHandler for p in parameter_handlers]):
            bond_handler = [p for p in parameter_handlers if type(p) == BondHandler][0]
            bonds = SMIRNOFFBondCollection.create(
                parameter_handler=bond_handler,
                topology=topology,
            )
        else:
            bond_handler = None
            bonds = None

        for key, match in constraint_matches.items():
            topology_key = BondKey(atom_indices=key)
            smirks = match.parameter_type.smirks
            distance = match.parameter_type.distance
            if distance is not None:
                # This constraint parameter is fully specified
                potential_key = PotentialKey(
                    id=smirks,
                    associated_handler="Constraints",
                )
                self.key_map[topology_key] = potential_key
                distance = match.parameter_type.distance
            else:
                # This constraint parameter depends on the BondHandler ...
                if bond_handler is None:
                    raise MissingParametersError(
                        f"Constraint with SMIRKS pattern {smirks} found with no distance "
                        "specified, and no corresponding bond parameters were found. The distance "
                        "of this constraint is not specified.",
                    )
                # ... so use the same PotentialKey instance as the BondHandler to look up the distance
                potential_key = bonds.key_map[topology_key]  # type: ignore[union-attr]
                self.key_map[topology_key] = potential_key
                distance = bonds.potentials[potential_key].parameters["length"]  # type: ignore[union-attr]
            potential = Potential(
                parameters={
                    "distance": distance,
                },
            )
            self.constraints[potential_key] = potential  # type: ignore[assignment]


class SMIRNOFFAngleCollection(SMIRNOFFCollection):
    """Handler storing angle potentials as produced by a SMIRNOFF force field."""

    type: Literal["Angles"] = "Angles"
    expression: Literal[
        "k/2*(theta-angle)**2",
        "k/2*(cos(theta)-cos(angle))**2",
    ] = "k/2*(theta-angle)**2"

    @classmethod
    def allowed_parameter_handlers(cls):
        """Return a list of allowed types of ParameterHandler classes."""
        return [AngleHandler]

    @classmethod
    def supported_parameters(cls):
        """Return a list of supported parameter attributes."""
        return ["smirks", "id", "k", "angle"]

    @classmethod
    def potential_parameters(cls):
        """Return a list of names of parameters included in each potential in this colletion."""
        return ["k", "angle"]

    @classmethod
    def valence_terms(cls, topology):
        """Return all angles in this topology."""
        return list(topology.angles)

    def store_potentials(self, parameter_handler: AngleHandler) -> None:
        """
        Populate self.potentials with key-val pairs of [AngleKey, PotentialKey].

        """
        for potential_key in self.key_map.values():
            smirks = potential_key.id
            parameter = parameter_handler.parameters[smirks]
            potential = Potential(
                parameters={
                    parameter_name: getattr(parameter, parameter_name)
                    for parameter_name in self.potential_parameters()
                },
            )
            self.potentials[potential_key] = potential


class SMIRNOFFProperTorsionCollection(SMIRNOFFCollection):
    """Handler storing proper torsions potentials as produced by a SMIRNOFF force field."""

    type: Literal["ProperTorsions"] = "ProperTorsions"
    expression: Literal[
        "k*(1+cos(periodicity*theta-phase))"
    ] = "k*(1+cos(periodicity*theta-phase))"
    fractional_bond_order_method: Literal["AM1-Wiberg"] = "AM1-Wiberg"
    fractional_bond_order_interpolation: Literal["linear"] = "linear"

    @classmethod
    def allowed_parameter_handlers(cls):
        """Return a list of allowed types of ParameterHandler classes."""
        return [ProperTorsionHandler]

    @classmethod
    def supported_parameters(cls):
        """Return a list of supported parameter attribute names."""
        return ["smirks", "id", "k", "periodicity", "phase", "idivf", "k_bondorder"]

    @classmethod
    def potential_parameters(cls):
        """Return a list of names of parameters included in each potential in this colletion."""
        return ["k", "periodicity", "phase", "idivf"]

    def store_matches(
        self,
        parameter_handler: ProperTorsionHandler,
        topology: Topology,
    ) -> None:
        """
        Populate self.key_map with key-val pairs of slots and unique potential identifiers.

        """
        if self.key_map:
            self.key_map: Dict[ProperTorsionKey, PotentialKey] = dict()  # type: ignore[assignment]
        matches = parameter_handler.find_matches(topology)
        for key, val in matches.items():
            param = val.parameter_type
            n_terms = len(val.parameter_type.phase)
            for n in range(n_terms):
                smirks = param.smirks
                if param.k_bondorder:
                    # The relevant bond order is that of the _central_ bond in the torsion
                    bond = topology.get_bond_between(key[1], key[2])
                    fractional_bond_order = bond.fractional_bond_order
                    if not fractional_bond_order:
                        raise RuntimeError(
                            "Bond orders should already be assigned at this point",
                        )
                else:
                    fractional_bond_order = None
                topology_key = ProperTorsionKey(
                    atom_indices=key,
                    mult=n,
                    bond_order=fractional_bond_order,
                )
                potential_key = PotentialKey(
                    id=smirks,
                    mult=n,
                    associated_handler="ProperTorsions",
                    bond_order=fractional_bond_order,
                )
                self.key_map[topology_key] = potential_key

        _check_all_valence_terms_assigned(
            handler=parameter_handler,
            topology=topology,
            assigned_terms=matches,
            valence_terms=list(topology.propers),
        )

    def store_potentials(self, parameter_handler: ProperTorsionHandler) -> None:
        """
        Populate self.potentials with key-val pairs of [ProperTorsionKey, PotentialKey].

        """
        for topology_key, potential_key in self.key_map.items():
            smirks = potential_key.id
            n = potential_key.mult
            parameter = parameter_handler.parameters[smirks]
            # n_terms = len(parameter.k)
            if topology_key.bond_order:
                bond_order = topology_key.bond_order
                data = parameter.k_bondorder[n]
                coeffs = _get_interpolation_coeffs(
                    fractional_bond_order=bond_order,
                    data=data,
                )
                pots = []
                map_keys = [*data.keys()]
                for map_key in map_keys:
                    parameters = {
                        "k": parameter.k_bondorder[n][map_key],
                        "periodicity": parameter.periodicity[n] * unit.dimensionless,
                        "phase": parameter.phase[n],
                        "idivf": parameter.idivf[n] * unit.dimensionless,
                    }
                    pots.append(
                        Potential(
                            parameters=parameters,
                            map_key=map_key,
                        ),
                    )
                potential = WrappedPotential(
                    {pot: coeff for pot, coeff in zip(pots, coeffs)},
                )
            else:
                parameters = {
                    "k": parameter.k[n],
                    "periodicity": parameter.periodicity[n] * unit.dimensionless,
                    "phase": parameter.phase[n],
                    "idivf": parameter.idivf[n] * unit.dimensionless,
                }
                potential = Potential(parameters=parameters)  # type: ignore[assignment]
            self.potentials[potential_key] = potential

    @classmethod
    def create(
        cls: Type[T],
        parameter_handler: ProperTorsionHandler,
        topology: Topology,
        partial_bond_orders_from_molecules=None,
    ) -> T:
        """
        Create a SMIRNOFFProperTorsionCollection from toolkit data.

        """
        handler: T = cls(
            type="ProperTorsions",
            expression="k*(1+cos(periodicity*theta-phase))",
            fractional_bond_order_method=parameter_handler.fractional_bondorder_method,
            fractional_bond_order_interpolation=parameter_handler.fractional_bondorder_interpolation,
        )

        if any(
            getattr(p, "k_bondorder", None) is not None
            for p in parameter_handler.parameters
        ):
            for ref_mol in topology.unique_molecules:
                if _check_partial_bond_orders(
                    ref_mol,
                    partial_bond_orders_from_molecules,
                ):
                    continue
                # TODO: expose conformer generation and fractional bond order assigment knobs via API?
                ref_mol.generate_conformers(n_conformers=1)
                ref_mol.assign_fractional_bond_orders(
                    bond_order_model=handler.fractional_bond_order_method.lower(),
                )

        handler.store_matches(parameter_handler=parameter_handler, topology=topology)
        handler.store_potentials(parameter_handler=parameter_handler)

        return handler


class SMIRNOFFImproperTorsionCollection(SMIRNOFFCollection):
    """Handler storing improper torsions potentials as produced by a SMIRNOFF force field."""

    type: Literal["ImproperTorsions"] = "ImproperTorsions"
    expression: Literal[
        "k*(1+cos(periodicity*theta-phase))"
    ] = "k*(1+cos(periodicity*theta-phase))"
    # TODO: Consider whether or not default_idivf should be stored here

    @classmethod
    def allowed_parameter_handlers(cls):
        """Return a list of allowed types of ParameterHandler classes."""
        return [ImproperTorsionHandler]

    @classmethod
    def supported_parameters(cls):
        """Return a list of supported parameter attribute names."""
        return ["smirks", "id", "k", "periodicity", "phase", "idivf"]

    @classmethod
    def potential_parameters(cls):
        """Return a list of names of parameters included in each potential in this colletion."""
        return ["k", "periodicity", "phase", "idivf"]

    def store_matches(
        self,
        parameter_handler: ImproperTorsionHandler,
        topology: Topology,
    ) -> None:
        """
        Populate self.key_map with key-val pairs of slots and unique potential identifiers.

        """
        if self.key_map:
            self.key_map = dict()
        matches = parameter_handler.find_matches(topology)
        for key, val in matches.items():
            parameter_handler._assert_correct_connectivity(
                val,
                [
                    (0, 1),
                    (1, 2),
                    (1, 3),
                ],
            )
            n_terms = len(val.parameter_type.k)
            for n in range(n_terms):
                smirks = val.parameter_type.smirks
                non_central_indices = [key[0], key[2], key[3]]

                for permuted_key in [
                    (
                        non_central_indices[i],
                        non_central_indices[j],
                        non_central_indices[k],
                    )
                    for (i, j, k) in [(0, 1, 2), (1, 2, 0), (2, 0, 1)]
                ]:
                    topology_key = ImproperTorsionKey(
                        atom_indices=(key[1], *permuted_key),
                        mult=n,
                    )
                    potential_key = PotentialKey(
                        id=smirks,
                        mult=n,
                        associated_handler="ImproperTorsions",
                    )
                    self.key_map[topology_key] = potential_key

    def store_potentials(self, parameter_handler: ImproperTorsionHandler) -> None:
        """
        Populate self.potentials with key-val pairs of [ImproperTorsionKey, PotentialKey].

        """
        _default_idivf = parameter_handler.default_idivf

        for potential_key in self.key_map.values():
            smirks = potential_key.id
            n = potential_key.mult
            parameter = parameter_handler.parameters[smirks]
            if parameter.idivf is None:
                idivf = None
            else:
                # Assumed to be list here
                idivf = parameter.idivf[n]
                if idivf is not None:
                    idivf = idivf * unit.dimensionless

            if idivf is None:
                if _default_idivf == "auto":
                    idivf = 3.0 * unit.dimensionless
                else:
                    # Assumed to be a numerical value
                    idivf = _default_idivf * unit.dimensionless

            parameters = {
                "k": parameter.k[n],
                "periodicity": parameter.periodicity[n] * unit.dimensionless,
                "phase": parameter.phase[n],
                "idivf": idivf,
            }
            potential = Potential(parameters=parameters)
            self.potentials[potential_key] = potential
