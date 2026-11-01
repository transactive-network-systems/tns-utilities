"""
Enumerations the archive knows.

A column definition names its enumeration (``{"name": "state", "enumeration": "MarketState"}``) and the data manager
resolves the name against the enumerations registered with it. The application registers its Python Enum classes at
start-up; a receiver that has no Python class for an enumeration (a historian) receives its definition over the wire
beside the table definitions and gets a dynamically created Enum, so decoding by name works identically on both sides.
Values are stored by member name, which is why a definition carries the member names in order and nothing else.

An enumeration definition is ``{"name": "MarketState", "members": ["Initial", "Active", ...]}``.
"""
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum


def enumeration_definition(enumeration: type[Enum]) -> dict:
    """The wire form of an Enum class: its name and its member names in definition order."""
    if not (isinstance(enumeration, type) and issubclass(enumeration, Enum)):
        raise TypeError(f'An enumeration must be an Enum class, not {enumeration!r}.')
    return {'name': enumeration.__name__, 'members': [member.name for member in enumeration]}


def enumeration_definitions(enumerations: Iterable[type[Enum]]) -> list[dict]:
    return [enumeration_definition(enumeration) for enumeration in enumerations]


def enumeration_from_definition(definition: Mapping) -> type[Enum]:
    """
    An Enum class built from a definition received over the wire. Members are numbered 1.. in the order given; only the
    names matter, since values are stored and sent by name. A definition without a usable name or without members is
    an error.
    """
    if not isinstance(definition, Mapping):
        raise ValueError(f'An enumeration definition must be a mapping, not {type(definition).__name__}.')
    unexpected = set(definition) - {'name', 'members'}
    if unexpected:
        raise ValueError(f'Enumeration definition {definition.get("name")!r} has unsupported key(s) '
                         f'{sorted(unexpected)}; it has "name" and "members".')
    name = definition.get('name')
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError(f'An enumeration definition needs a name that is an identifier; got {name!r}.')
    members = definition.get('members')
    if (not isinstance(members, Sequence) or isinstance(members, (str, bytes)) or not members
            or not all(isinstance(member, str) and member.isidentifier() for member in members)):
        raise ValueError(f'Enumeration {name!r} needs a non-empty list of member names (identifiers); got {members!r}.')
    if len(set(members)) != len(members):
        raise ValueError(f'Enumeration {name!r} lists a member twice: {list(members)}.')
    return Enum(name, list(members))


class EnumerationRegistry:
    """The enumerations a data manager resolves column definitions against, by name."""

    def __init__(self):
        self._enumerations: dict[str, type[Enum]] = {}

    def __contains__(self, name: str) -> bool:
        return name in self._enumerations

    def __len__(self) -> int:
        return len(self._enumerations)

    def names(self) -> list[str]:
        return sorted(self._enumerations)

    def get(self, name: str) -> type[Enum]:
        """The registered enumeration `name`; a KeyError naming the known enumerations when there is none."""
        try:
            return self._enumerations[name]
        except KeyError:
            raise KeyError(f'Unknown enumeration {name!r}; the registered enumerations are {self.names()}. Register it '
                           f'with the data manager (register_enumerations) before the tables that use it.') from None

    def register(self, enumeration: type[Enum]) -> type[Enum]:
        """
        Register an Enum class. Registering the same name again is accepted when the member names agree (the same
        class, or a dynamic twin received over the wire); a different member list under a known name is an error, since
        rows already decoded with one would be misread with the other.
        """
        definition = enumeration_definition(enumeration)
        existing = self._enumerations.get(definition['name'])
        if existing is None:
            self._enumerations[definition['name']] = enumeration
            return enumeration
        if enumeration_definition(existing)['members'] != definition['members']:
            raise ValueError(f'Enumeration {definition["name"]!r} is already registered with members '
                             f'{enumeration_definition(existing)["members"]}; the new one has {definition["members"]}.')
        return existing

    def register_definition(self, definition: Mapping) -> type[Enum]:
        """Register an enumeration received as a definition; the registered class is returned (the existing one when
        the name is known with the same members)."""
        return self.register(enumeration_from_definition(definition))

    def definitions(self) -> list[dict]:
        """Every registered enumeration in wire form, sorted by name."""
        return [enumeration_definition(self._enumerations[name]) for name in self.names()]
