"""The configuration contract: validation in the constructor, unknown keys, reconfiguration of marked fields."""
import pytest

from archivable import ConfigModel, Configurable, ConfigurationError, ReconfigurationError, Setting
from archivable.config import RECONFIGURABLE


class Widget(Configurable):
    class Config(ConfigModel):
        size: int = Setting(1, ge=1, description='[m] the size.')
        label: str = Setting('', reconfigurable=True)
        colour: str = Setting('red', reconfigurable=True, json_schema_extra={'x-demo': True})

    def __init__(self, config=None, **kwargs):
        cfg = self._configure(config, kwargs)
        self.size, self.label, self.colour = cfg.size, cfg.label, cfg.colour


class BigWidget(Widget):
    class Config(Widget.Config):
        depth: int = Setting(2)

    def __init__(self, config=None, **kwargs):
        cfg = self._configure(config, kwargs)
        super().__init__(config=cfg)
        self.depth = cfg.depth


def test_keywords_are_validated_and_read_from_the_model():
    widget = Widget(size=3, label='a')
    assert (widget.size, widget.label, widget.config_version) == (3, 'a', 1)
    assert widget.config.model_dump() == {'size': 3, 'label': 'a', 'colour': 'red'}


def test_unknown_keys_and_bad_values_name_the_class_and_the_key():
    with pytest.raises(ConfigurationError, match="Widget: unknown configuration key 'width'"):
        Widget(width=2)
    with pytest.raises(ConfigurationError, match='size'):
        Widget(size=0)
    with pytest.raises(ConfigurationError, match='dictionary of settings'):
        Widget.Config.validate_for('Widget', ['not', 'a', 'mapping'])


def test_a_subclass_validates_once_and_hands_the_instance_down():
    widget = BigWidget(size=2, depth=5)
    assert (widget.size, widget.depth) == (2, 5) and isinstance(widget.config, BigWidget.Config)
    with pytest.raises(TypeError, match='needs a BigWidget.Config'):
        BigWidget(config=Widget.Config())
    with pytest.raises(TypeError, match='both a validated configuration and keywords'):
        Widget(config=Widget.Config(), size=2)


def test_reconfigurable_fields_are_marked_and_the_marker_merges_with_other_extras():
    assert Widget.Config.reconfigurable_fields() == {'label', 'colour'}
    extra = Widget.Config.model_fields['colour'].json_schema_extra
    assert extra == {'x-demo': True, RECONFIGURABLE: True}
    assert Widget.Config.model_json_schema()['properties']['label'][RECONFIGURABLE] is True


def test_reconfigure_applies_marked_fields_and_refuses_the_rest():
    widget = Widget()
    widget.reconfigure({'label': 'new', 'colour': 'blue'})
    assert (widget.label, widget.colour, widget.config_version) == ('new', 'blue', 2)
    with pytest.raises(ReconfigurationError, match=r"\['size'\] cannot be changed"):
        widget.reconfigure({'size': 4})
    with pytest.raises(ReconfigurationError, match="'width'.*not a configuration key at all"):
        widget.reconfigure({'width': 4})
    assert widget.size == 1 and widget.config_version == 2


def test_diff_configuration_and_validate_spec():
    widget = Widget(size=2, label='x')
    assert widget.diff_configuration({'size': 2, 'label': 'y'}) == {'label': 'y'}
    cfg = Widget.validate_spec({'class_name': 'Widget', 'module_name': 'demo', 'size': 7})
    assert cfg.size == 7
    assert Widget.declares_config() and BigWidget.declares_config()

    class Plain(Widget):
        pass
    assert not Plain.declares_config()
    assert Widget.describe({'name': 'w1'}) == "Widget 'w1'" and Widget.describe() == 'Widget'
