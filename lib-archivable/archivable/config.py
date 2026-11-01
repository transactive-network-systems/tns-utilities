"""
The configuration contract of configurable classes.

A configurable class (a data manager here; every configurable component of an application built on this library)
declares a nested ``Config`` model built on ConfigModel below. The constructor validates its keyword arguments through
that model and reads its settings from the validated instance, so the declaration is the code: a key the model does not
know is an error at configuration time, naming the class and the key, and the JSON Schema the models produce cannot
drift from what the constructors accept.

Fields a running object may change in place are marked reconfigurable (``Setting(..., reconfigurable=True)``, which
puts ``{"reconfigurable": true}`` in the field's JSON Schema extra); ``Config.reconfigurable_fields()`` lists them and
``Configurable.reconfigure`` applies a change to them, refusing anything else.

An application with richer conventions (durations in seconds, nested class specifications, ...) subclasses ConfigModel
and Configurable and extends ``Setting``; its models stay instances of these classes.
"""
import logging
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_log = logging.getLogger(__name__)

__all__ = ['ConfigModel', 'ConfigurationError', 'ReconfigurationError', 'Configurable', 'Setting', 'RECONFIGURABLE',
           'field_extra']

# Key of Field.json_schema_extra marking a field a running object may change in place.
RECONFIGURABLE = 'reconfigurable'


# --------------------------------------------------------------------------------------------------------- errors

class ConfigurationError(ValueError):
    """A configuration the models reject. The message names the owner (class and configured name) and every offending
    key, so the file can be corrected without reading a traceback."""

    def __init__(self, owner: str, problems: list[str]):
        self.owner = owner
        self.problems = list(problems)
        super().__init__(f'{owner}: ' + '; '.join(self.problems))

    @classmethod
    def from_validation(cls, owner: str, error: ValidationError, model: type[BaseModel]) -> 'ConfigurationError':
        problems = []
        unknown = []
        for detail in error.errors():
            location = '.'.join(str(part) for part in detail['loc'])
            if detail['type'] == 'extra_forbidden':
                unknown.append(location)
            elif detail['type'] == 'missing':
                problems.append(f'{location} is required')
            else:
                message = detail['msg']
                if message.startswith('Value error, '):
                    message = message[len('Value error, '):]
                problems.append(f'{location}: {message}' if location else message)
        if unknown:
            known = sorted(info.alias or name for name, info in model.model_fields.items())
            problems.insert(0, f'unknown configuration key{"s" if len(unknown) > 1 else ""} '
                               f'{", ".join(repr(key) for key in unknown)} (the keys of {model.__qualname__} are '
                               f'{known})')
        return cls(owner, problems)


class ReconfigurationError(ValueError):
    """A change a running object cannot take in place: it names a field that is not reconfigurable (the object must be
    rebuilt for it), or the changed values do not validate."""


# ---------------------------------------------------------------------------------------------------------- fields

def Setting(default: Any = ..., *, reconfigurable: bool = False, **field_kwargs: Any) -> Any:
    """
    Field() with the ``reconfigurable`` marker in json_schema_extra when a running object may change the setting in
    place. Everything else (default, description, ge, gt, json_schema_extra, ...) is passed to Field. Units belong in
    the description, in brackets.
    """
    if reconfigurable:
        existing = field_kwargs.pop('json_schema_extra', None) or {}
        field_kwargs['json_schema_extra'] = {**existing, RECONFIGURABLE: True}
    return Field(default, **field_kwargs)


def field_extra(info) -> dict:
    """The json_schema_extra of a field as a dict (empty when it is unset or a callable)."""
    extra = info.json_schema_extra
    return extra if isinstance(extra, dict) else {}


# ------------------------------------------------------------------------------------------------------ the models

class ConfigModel(BaseModel):
    """Base of every configuration model: unknown keys are errors, Python objects may be given where code wires things
    directly, defaults are written in configuration form and validated like given values, and the markers set through
    Setting() are readable."""
    model_config = ConfigDict(extra='forbid', arbitrary_types_allowed=True, validate_default=True)

    @classmethod
    def reconfigurable_fields(cls) -> frozenset[str]:
        """The fields a running object may change in place."""
        return frozenset(name for name, info in cls.model_fields.items() if field_extra(info).get(RECONFIGURABLE))

    @classmethod
    def validate_for(cls, owner: str, data: Mapping) -> 'ConfigModel':
        """model_validate(data), reporting failures as a ConfigurationError that names `owner`."""
        if not isinstance(data, Mapping):
            raise ConfigurationError(owner, [f'the configuration must be a dictionary of settings, not '
                                             f'{type(data).__name__}: {data!r}'])
        try:
            return cls.model_validate(dict(data))
        except ValidationError as error:
            raise ConfigurationError.from_validation(owner, error, cls) from None

    def changed_fields(self, other: 'ConfigModel') -> set[str]:
        """The names of the fields whose values differ between this configuration and `other` (same model)."""
        return {name for name in type(self).model_fields if getattr(self, name) != getattr(other, name)}


# ----------------------------------------------------------------------------------------------------- the mixin

class Configurable:
    """
    What a class with a nested Config model gets: validation of its keyword arguments in the constructor (see
    _configure), the validated configuration as ``config``, and in-place reconfiguration of the fields its Config marks
    reconfigurable (reconfigure). Subclass models extend the parent's model; a subclass constructor validates once and
    hands the validated instance down as ``config=``, so the parent reads the same object instead of validating again.
    """
    Config: ClassVar[type[ConfigModel]] = ConfigModel
    # Keys of a specification dict that are not settings of the Config (consumed by whoever builds the object).
    RUNTIME_SPEC_KEYS: ClassVar[frozenset[str]] = frozenset({'class_name', 'module_name'})

    config: ConfigModel
    config_version: int

    @classmethod
    def declares_config(cls) -> bool:
        """True when this class declares its own Config (``Config = Parent.Config`` when nothing is added). A class
        that only extends a configurable constructor without declaring one is validated by the base's Config for the
        keywords that reach it, and is not pre-validated by its host."""
        return 'Config' in vars(cls)

    @classmethod
    def describe(cls, data: Mapping | None = None, name: str | None = None) -> str:
        """'ClassName' or "ClassName 'configured name'" for messages."""
        if name is None and isinstance(data, Mapping):
            name = data.get('name')
        return f'{cls.__name__} {name!r}' if name else cls.__name__

    @classmethod
    def validate_config(cls, data: Mapping, name: str | None = None) -> ConfigModel:
        """The validated Config for the settings `data`; a ConfigurationError names this class and `name`."""
        return cls.Config.validate_for(cls.describe(data, name), data)

    @classmethod
    def validate_spec(cls, spec: Mapping) -> ConfigModel:
        """
        Validate a configuration dictionary that names this class (class_name, module_name and the settings), and the
        nested specifications its fields hold (validate_nested_specs), without building anything.
        """
        settings = {key: value for key, value in spec.items() if key not in cls.RUNTIME_SPEC_KEYS}
        cfg = cls.validate_config(settings)
        cls.validate_nested_specs(cfg, cls.describe(settings))
        return cfg

    @classmethod
    def validate_nested_specs(cls, cfg: ConfigModel, owner: str) -> None:
        """Hook for applications whose models nest specifications of other configurable classes. The library's
        configurations nest none."""
        return None

    def _configure(self, config: ConfigModel | None, kwargs: Mapping) -> ConfigModel:
        """
        The validated configuration of this object: `config` when a subclass constructor already validated it (it must
        then be an instance of this class's Config and `kwargs` must be empty), otherwise `kwargs` validated through
        this class's Config. Stored as ``self.config`` (version 1).
        """
        if config is not None:
            if not isinstance(config, self.Config):
                raise TypeError(f'{type(self).__name__} was given a {type(config).__qualname__} as its configuration; '
                                f'it needs a {self.Config.__qualname__}.')
            if kwargs:
                raise TypeError(f'{type(self).__name__} was given both a validated configuration and keywords '
                                f'{sorted(kwargs)}; give one or the other.')
            cfg = config
        else:
            cfg = self.validate_config(kwargs)
        if getattr(self, 'config', None) is None:
            self.config = cfg
            self.config_version = 1
        return cfg

    # ------------------------------------------------------------------------------------------- reconfiguration

    def check_reconfigure(self, changes: Mapping) -> ConfigModel:
        """
        The configuration this object would have after `changes`, validated, without applying it. Raises
        ReconfigurationError when a changed field is not reconfigurable (listing them) or the new values do not
        validate.
        """
        changes = dict(changes)
        refused = sorted(set(changes) - self.Config.reconfigurable_fields())
        if refused:
            unknown = sorted(set(refused) - set(self.Config.model_fields))
            detail = (f' ({", ".join(repr(k) for k in unknown)} {"are" if len(unknown) > 1 else "is"} not a '
                      f'configuration key at all)' if unknown else '')
            raise ReconfigurationError(
                f'{self.describe(name=getattr(self, "name", None))}: {refused} cannot be changed on a running '
                f'object{detail}; reconfigurable fields are {sorted(self.Config.reconfigurable_fields())}. '
                f'Rebuild it for other changes.')
        current = self.config.model_dump()
        try:
            return self.validate_config({**current, **changes}, name=getattr(self, 'name', None))
        except ConfigurationError as error:
            raise ReconfigurationError(str(error)) from None

    def reconfigure(self, changes: Mapping) -> None:
        """Change the reconfigurable fields in `changes` in place: validate (check_reconfigure), apply, and record
        the new configuration version."""
        new_config = self.check_reconfigure(changes)
        changed = set(changes)
        self._apply_reconfiguration(new_config, changed)
        self.config = new_config
        self.config_version += 1
        _log.info(f'{self.describe(name=getattr(self, "name", None))} reconfigured {sorted(changed)} '
                  f'(configuration version {self.config_version}).')
        self._configuration_changed(changed)

    def diff_configuration(self, settings: Mapping) -> dict:
        """The changes (field -> new value) that would take this object from its current configuration to `settings`
        (a full set of settings, as configured). Settings `settings` leaves out revert to their defaults."""
        new_config = self.validate_config(settings, name=getattr(self, 'name', None))
        return {name: getattr(new_config, name) for name in self.config.changed_fields(new_config)}

    def _apply_reconfiguration(self, cfg: ConfigModel, changed: set[str]) -> None:
        """Apply the changed fields of `cfg` to this object. The base behaviour sets the attribute of the same name;
        classes that derive other state from a field extend this."""
        for field_name in changed:
            setattr(self, field_name, getattr(cfg, field_name))

    def _configuration_changed(self, changed: set[str]) -> None:
        """Hook after a reconfiguration is applied (an application may archive the new version here)."""
        return None

    def configuration_record(self) -> dict:
        """The current configuration as JSON-ready data (for the archive and for comparison)."""
        return self.config.model_dump(mode='json')
