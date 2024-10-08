"""Functions and coroutines for automating liquid movement.

.. versionadded:: 1.71.0

Example
-------

    import functools as ft

    import dropbot as db
    import dropbot.move
    import trollius as asyncio

    route = [110, 109, 115, 114, 115, 109, 110]

    proxy = db.SerialProxy()

    # Set actuation voltage to use.
    proxy.voltage = 115
    # Set DropBot capacitance updated interval to 25 ms.
    proxy.update_state(capacitance_update_interval_ms=25)
    # Apply each actuation for at least 0.3 seconds; allow up to 5
    # seconds of actuation before attempting to retry.
    task = db.move.move_liquid(proxy, route, min_duration=.3,
                               wrapper=ft.partial(asyncio.wait_for, timeout=5))
    loop = asyncio.get_event_loop()
    # Collect DropBot `capacitance-updated` messages.
    messages = loop.run_until_complete(task)
    # Disable DropBot capacitance updates.
    proxy.update_state(capacitance_update_interval_ms=0)
    proxy.turn_off_all_channels()
"""
import asyncio
import logging

import numpy as np
import pandas as pd
import networkx as nx
import functools as ft
import itertools as it

from typing import Optional, Union

from .proxy import SerialProxy, dropbot_state, EVENT_CHANNELS_UPDATED

__all__ = ['MoveTimeout', 'actuate', 'actuate_channels', 'gather_liquid',
           'load', 'move_liquid', 'move_results_to_frame', 'test_steady_state',
           'wait_on_capacitance', 'window']


class MoveTimeout(asyncio.TimeoutError):
    """
    Exception occurred while performing move along route.

    Attributes
    ----------
    route : list[int]
        List of channels along route.
    route_i : tuple[int, int]
        Source and target channel of failed move.
    """

    def __init__(self, route, route_i, *args, **kwargs):
        self.route = route
        self.route_i = route_i
        super().__init__(*args, **kwargs)


def window(seq: Union[list, np.array], n: int):
    """
    Returns
    -------
    iter
        Sliding window (of width n) over data from the iterable::

            s -> (s0,s1,...s[n-1]), (s1,s2,...,sn), ...
    """
    it_ = iter(seq)
    result = tuple(it.islice(it_, n))
    if len(result) == n:
        yield result
    for elem in it_:
        result = result[1:] + (elem,)
        yield result


async def wait_on_capacitance(proxy_: SerialProxy, callback: callable) -> list:
    """
    Return once callback returns `True`.

    Parameters
    ----------
    proxy_: dropbot.SerialProxy
    callback
        Callback function accepting a list of ``capacitance-updated`` messages
        as only argument.

    Returns
    -------
    list
        List of DropBot ``capacitance-updated`` messages containing the
        following keys::

         - ``event``: ``"capacitance-updated"``
         - ``new_value``: capacitance value in Farads
         - ``time_us``: DropBot microsecond 32-bit counter
         - ``n_samples``: number of samples used for RMS measurement
         - ``V_a``: measured actuation voltage during capacitance reading
    """
    move_done = asyncio.Event()
    loop = asyncio.get_event_loop()

    messages = []

    def _on_capacitance(message):
        # message.keys == ['event', 'new_value', 'time_us', 'n_samples', 'V_a']
        # Added by `co_target_capacitance()`: 'actuation_uuid1', 'actuated_channels'
        try:
            messages.append(message)
            if callback(messages):
                loop.call_soon_threadsafe(move_done.set)
        except Exception:
            logging.debug('capacitance event error.', exc_info=True)
            return

    proxy_.signals.signal('capacitance-updated').connect(_on_capacitance)
    await move_done.wait()
    return messages


async def actuate_channels(proxy_: SerialProxy, channels: Union[list, np.array],
                           allow_disabled: Optional[bool] = True) -> list:
    """
    Parameters
    ----------
    proxy_: dropbot.SerialProxy
    channels : list
        List of channel numbers to actuate.
    allow_disabled : bool, optional
        If ``False``, verify actuated channels match specified channels
        _exactly_.  Otherwise, ensure that all actuated channels belong to the
        specified set of channels, _even if_ not _all_ specified channels are
        actuated.  This supports attempting to actuate channels that are
        disabled.

    Returns
    -------
    list
        List of actuated channels.  If :data:`allow_disabled` is ``True``, the
        returned list of channels may differ from the specified list of
        channels.

    Raises
    ------
    RuntimeError
        If list actuated channels does not match the requested channels
        (missing disabled channels are ignored if ``allowed_disabled`` is
        `True`).
    """
    loop = asyncio.get_event_loop()

    channels_updated = asyncio.Event()

    def _on_channels_updated(message):
        channels_updated.actuated = message.get('actuated')
        loop.call_soon_threadsafe(channels_updated.set)

    # Enable `channels-updated` DropBot signal.
    proxy_.enable_event(EVENT_CHANNELS_UPDATED)

    # Request to be notified when the set of actuated channels changes.
    signal = proxy_.signals.signal('channels-updated')
    signal.connect(_on_channels_updated)

    # Request actuation of the specified channels.
    proxy_.set_state_of_channels(pd.Series(1, index=channels), append=False)

    await channels_updated.wait()
    if not allow_disabled and (set(channels_updated.actuated) !=
                               set(channels)):
        raise RuntimeError(f'Actuated channels `{channels_updated.actuated}` do not match '
                           f'expected channels `{channels}`')
    elif set(channels_updated.actuated) - set(channels):
        # Disabled channels are allowed.
        raise RuntimeError(f'Actuated channels `{channels_updated.actuated}` are not included in'
                           f' expected channels `{channels}`')
    return channels_updated.actuated


def test_steady_state(messages: list, std_error: Optional[float] = .02,
                      min_duration: Optional[float] = .3, threshold: Optional[float] = 10e-12) -> bool:
    """
    Callback to check for capacitance steady state.

    Parameters can be set using `functools.partial()`.

    Parameters
    ----------
    messages : list
        List of DropBot ``capacitance-updated`` messages containing the
        following keys::

         - ``event``: ``"capacitance-updated"``
         - ``new_value``: capacitance value in Farads
         - ``time_us``: DropBot microsecond 32-bit counter
         - ``n_samples``: number of samples used for RMS measurement
         - ``V_a``: measured actuation voltage during capacitance reading
    std_error : float, optional
        Ratio of the standard deviation (from most recent 100 samples) to the
        median under which steady state is considered to be reached _(default:
        0.02, i.e., 2% of the median)_.
    min_duration : float, optional
        Minimum time (in seconds) since first capacitance update before
        considering steady state as reached (default: 0.3).
    threshold : float, optional
        Minimum median capacitance in Farads (from most recent 100 samples)
        before considering steady state as reached.
    """
    df = pd.DataFrame(messages[-100:])
    df['time'] = df.time_us * 1e-6
    df['time'] -= df.time.iloc[0]
    df.set_index('time', inplace=True)
    if (df.index.values[-1] - df.index.values[0]) < min_duration:
        return False
    start = df.index.values[-1] - min_duration
    std = df.new_value.loc[start:].std()
    q50 = df.new_value.loc[start:].quantile(q=0.5)
    result = (std / q50) < std_error
    return result and (q50 >= threshold)


async def actuate(proxy_: SerialProxy, channels: Union[list, np.array], callback: callable) -> list:
    """
    Actuate channels and wait for callback to return `True`.

    Parameters
    ----------
    proxy_: dropbot.SerialProxy
    channels : list
        List of channel numbers to actuate.
    callback
        Callback function accepting a list of ``capacitance-updated`` messages
        as only argument.
    """
    # Actuate channels.
    await actuate_channels(proxy_, channels)
    # Wait for callback.
    result = await wait_on_capacitance(proxy_, callback)
    return result


async def move_liquid(proxy_: SerialProxy, route: list, min_duration: Optional[float] = .3,
                      trail_length: Optional[int] = 1, wrapper: Optional[callable] = None) -> list:
    """
    Move liquid along specified route (i.e., list of channels).

    Parameters
    ----------
    proxy_: drobpot.SerialProxy
    route : list[int]
        Ordered sequence of channels to move along.
    min_duration : float, optional
        Minimum time to apply each actuation.
    trail_length : int, optional
        Number of electrodes to actuate at the same time along route.

        .. versionadded:: 1.72.0
    wrapper : callable, optional
        Function to wrap around calls to `actuate()`.

        Useful, for example, to apply an actuation timeout using
        `asyncio.wait_for()`.

    Returns
    -------
    list[dict]
        List of dictionaries, each corresponding to a single channel actuation
        with the following keys::

         - ``channels``: actuated channels
         - ``messages``: ``capacitance-updated`` messages received during the
           actuation


    .. versionchanged:: 1.72.0
        Add `trail_length` keyword argument.
    """
    if wrapper is None:
        def wrapper(task):
            return task

    messages_ = []

    duration = min_duration
    route_i = 0
    try:
        for route_i in window(route, trail_length + 1):
            print(f'\r{"Wait for steady state:":<50} {list(route_i)}', end='')
            messages = await wrapper(actuate(proxy_, route_i, ft.partial(test_steady_state,
                                                                         min_duration=duration)))
            messages_.append({'channels': tuple(route_i),
                              'messages': messages})
            head_channels_i = list(route_i[-trail_length:])
            print(f'\r{"Wait for steady state:":<50} {head_channels_i}', end='')
            messages = await wrapper(actuate(proxy_, head_channels_i, ft.partial(test_steady_state,
                                                                                 min_duration=duration)))
            messages_.append({'channels': tuple(head_channels_i),
                              'messages': messages})
    except (asyncio.CancelledError, asyncio.TimeoutError):
        raise MoveTimeout(route, route_i)

    return messages_


def move_results_to_frame(move_results: list) -> pd.DataFrame:
    """
    Convert results from `move_liquid()` to a data frame.

    The results from `move_liquid()` may be easily serialized as JSON.
    However, when attempting to, for example, plot the results, it is easier to
    manage the data in a data frame.

    Parameters
    ----------
    move_results : list
        Results returned by `move_liquid()` coroutine.

    Returns
    -------
    pandas.DataFrame
        For example::

                                                     event     new_value     time_us  n_samples      V_a
            channels         time (s)
              0 - (110, 109) 0.000000  capacitance-updated  7.639510e-11  2951675894         50  114.943
                             0.051765  capacitance-updated  7.559210e-11  2951727659         50  115.910
                             0.103516  capacitance-updated  7.617850e-11  2951779410         50  114.729
                             0.129394  capacitance-updated  7.569810e-11  2951805288         50  114.913
                             0.181169  capacitance-updated  7.564950e-11  2951857063         50  114.696

    Example
    -------

        import functools as ft

        import dropbot as db
        import dropbot.move
        import trollius as asyncio

        route = [110, 109, 115, 114, 115, 109, 110]

        proxy = db.SerialProxy()

        # Set actuation voltage to use.
        proxy.voltage = 115
        # Set DropBot capacitance updated interval to 25 ms.
        proxy.update_state(capacitance_update_interval_ms=25)
        # Apply each actuation for at least 0.3 seconds; allow up to 5
        # seconds of actuation before attempting to retry.
        task = db.move.move_liquid(proxy, route, min_duration=.3,
                                   wrapper=ft.partial(asyncio.wait_for,
                                   timeout=5))
        loop = asyncio.get_event_loop()
        # Collect DropBot `capacitance-updated` messages.
        messages = loop.run_until_complete(task)
        # Disable DropBot capacitance updates.
        proxy.update_state(capacitance_update_interval_ms=0)
        proxy.turn_off_all_channels()

        df = db.move.move_results_to_frame(messages)
        # Scale width of figure along with test duration.
        width = int(df.index.get_level_values('time (s)')[-1])
        # Plot capacitance readings associated with each move in a different
        # colour.
        axis = df.reset_index(level=0).groupby('channels').new_value\
            .plot(style='x', legend=False, figsize=(width, 10))[0]
        axis.set_ylim(0)
    """
    # Combine `capacitance-updated` messages collected during each move into a
    # single data frame.
    keys = []
    frames = []
    for i, message_i in enumerate(move_results):
        keys.append('%3d - %s' % (i, message_i['channels']))
        frames.append(pd.DataFrame(message_i['messages']))
    df = pd.concat(frames, keys=keys)
    df.index.levels[0].name = 'channels'
    df['time (s)'] = df['time_us'] * 1e-6
    df['time (s)'] -= df['time (s)'].iloc[0]
    df.set_index('time (s)', append=True, inplace=True)
    df.reset_index(level=1, drop=True, inplace=True)
    return df


async def load(proxy_: SerialProxy, channels: list, threshold: Optional[float] = 50e-12,
               load_duration: Optional[float] = .25, detach_duration: Optional[float] = 2.) -> list:
    """
    Load reservoir to specified threshold capacitance.

    Steps::

     1. Load: actuate first electrode until threshold capacitance is met.
     2. Detach: turn off first electrode and actuate the remaining electrodes
        until threshold capacitance is met.

    Parameters
    ----------
    proxy_: dropbot.SerialProxy
    channels : list
        List of channel numbers to actuate.
    threshold : float, optional
        Minimum median capacitance in Farads (from most recent ``N`` samples)
        before considering load complete (default: 50 pF).
    load_duration : float, optional
        Seconds before first electrode capacitance is considered stable
        (default: 0.25).
    detach_duration : float, optional
        Seconds before detaching electrodes capacitance is considered stable
        (default: 0.5).

    Returns
    -------
    list
        See `wait_on_capacitance()` for return type.
    """
    # Load starting reservoir.
    logging.debug(f'Wait for channel `{channels[:1]}` to be loaded')
    await actuate(proxy_, channels[:-1], ft.partial(test_steady_state,
                                                    min_duration=load_duration,
                                                    threshold=1.1 * threshold))

    detach_channels = channels[1:]
    logging.debug(f'Wait for liquid to detach from edge electrode `{channels[:1]}` to `{detach_channels}`...')
    messages = await actuate(proxy_, detach_channels, ft.partial(test_steady_state,
                                                                 min_duration=detach_duration,
                                                                 threshold=threshold))
    return messages


async def gather_liquid(proxy_: SerialProxy, graph: nx.Graph, sources: list, target: int,
                        wrapper: Optional[callable] = ft.partial(asyncio.wait_for, timeout=4),
                        update_interval: Optional[float] = .025) -> None:
    """
    Sequentially move liquid from each specified source to a shared target.

    Parameters
    ----------
    proxy_ : dropbot.SerialProxy
        DropBot serial handle.
    graph : networkx.Graph
        Channel/electrode connection graph.
    sources : list[int]
        List of source channel numbers.
    target : int
        Target channel number.
    wrapper : callable, optional
        Function to wrap around calls to `move_liquid()`.

        Useful, for example, to apply an actuation timeout using
        `asyncio.wait_for()`.
    update_interval : float, optional
        Capacitance update interval in seconds (default: 0.025).


    .. versionadded:: 1.72.0
    """
    with dropbot_state(proxy_, capacitance_update_interval_ms=int(update_interval * 1e3)):
        for source_i in sources:
            await move_liquid(proxy_, nx.shortest_path(graph, source_i, target), wrapper=wrapper)
