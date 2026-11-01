"""
Test doubles for the platform surface the data manager touches.

The tests run without a VOLTTRON platform. Before the package is imported, this conftest installs fake `volttron`
modules (always) and a fake `gevent` (when the real one is not importable) into sys.modules. The fakes cover the
modular platform names the package imports: volttron.client.vip.agent.errors and volttron.utils.jsonrpc.
"""
import sys
import types


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_fake_gevent():
    class Timeout(Exception):
        pass

    def sleep(seconds=0):
        pass

    class AsyncResult:
        """set() then get(); get() on an unset result raises Timeout at once (the fake never blocks)."""
        _unset = object()

        def __init__(self):
            self._value = self._unset

        def set(self, value=None):
            self._value = value

        def ready(self):
            return self._value is not self._unset

        def get(self, block=True, timeout=None):
            if self._value is self._unset:
                raise Timeout()
            return self._value

    class Greenlet:
        """Records the target; never runs it (a looping target would spin with the no-op sleep)."""
        def __init__(self, func, args, kwargs):
            self.func, self.args, self.kwargs = func, args, kwargs
            self.dead = False

        def kill(self, *args, **kwargs):
            self.dead = True

    def spawn(func, *args, **kwargs):
        return Greenlet(func, args, kwargs)

    gevent = _module('gevent', Timeout=Timeout, sleep=sleep, spawn=spawn, Greenlet=Greenlet)
    gevent.event = _module('gevent.event', AsyncResult=AsyncResult)


def _install_fake_volttron():
    class RemoteError(Exception):
        pass

    class VIPError(Exception):
        pass

    class Unreachable(VIPError):
        pass

    errors = _module('volttron.client.vip.agent.errors', VIPError=VIPError, Unreachable=Unreachable)
    volttron = _module('volttron')
    client = _module('volttron.client')
    vip = _module('volttron.client.vip')
    agent = _module('volttron.client.vip.agent', errors=errors)
    utils = _module('volttron.utils')
    _module('volttron.utils.jsonrpc', RemoteError=RemoteError)
    volttron.client, volttron.utils = client, utils
    client.vip = vip
    vip.agent = agent


try:
    import gevent  # noqa: F401
except ImportError:
    _install_fake_gevent()

# The tests never want a real platform: the fake modules are installed whether or not volttron is importable.
_install_fake_volttron()
