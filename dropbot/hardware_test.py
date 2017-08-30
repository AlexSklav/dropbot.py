from functools import wraps
import datetime as dt
import logging
import time
import uuid

from base_node import BaseNode
from dropbot import metadata
import json_tricks
import numpy as np
import path_helpers as ph


logger = logging.getLogger(name=__name__)


def restore_state(f):
    """
    Wrapper for restoring state after a test has completed..
    """
    @wraps(f)
    def _decorator(*args, **kwargs):
        proxy = args[0]
        # Save state of attributes that we will be modifying.
        state = {attr_i: getattr(proxy, attr_i)
                 for attr_i in ('hv_output_enabled', 'hv_output_selected',
                                'state_of_channels', 'voltage',
                                'frequency')}
        try:
            result = f(*args, **kwargs)
        finally:
            # Restore state of attributes that we were modified.
            for attr_i, value_i in state.iteritems():
                setattr(proxy, attr_i, value_i)
        return result
    return _decorator


def time_it(f):
    """
    Wrapper for timing each test and adding the duration and a
    utc timestamp.
    """
    @wraps(f)
    def _decorator(*args, **kwargs):
        start_time = time.time()
        result = f(*args, **kwargs)
        result['duration'] = time.time() - start_time
        result['utc_timestamp'] = dt.datetime.utcnow().isoformat()
        return result
    return _decorator


def log_results(results, output_dir):
    '''
    .. versionchanged:: 1.28
        Use ` `json_tricks.dumps``
        <http://json-tricks.readthedocs.io/en/latest/#dumps>`_ to dump results.

    Parameters
    ----------
    results : dict
        Test results.
    output_dir : str
        Path to output directory.
    '''
    output_dir = ph.path(output_dir)

    # Make output directory if it doesn't exist.
    output_dir.makedirs_p()

    # Construct filename based on current UTC date and time.
    filepath = output_dir.joinpath('results-%s.json' % dt.datetime.utcnow()
                                   .isoformat().replace(':', '.'))

    # write the results to a file
    with filepath.open('w') as output:
        # Use `json_tricks` to serialize `numpy` array **and** scalar data
        # types.
        json_tricks.dump(results, output)


@time_it
@restore_state
def system_info(proxy):
    '''
    Get system info (e.g., control board uuid, config,
    and properties, number of channels detected, and soft i2c scan.

    Parameters
    ----------
    proxy : Proxy

    Returns
    -------
    dict
        Nested dictionary containing system info.
    '''
    results = {}

    results['control board'] = {}
    results['control board']['uuid'] = str(proxy.uuid)
    results['control board']['properties'] = proxy.properties.to_dict()
    results['control board']['config'] = proxy.config.to_dict()
    results['soft_i2c_scan'] = proxy.soft_i2c_scan().tolist()
    results['number_of_channels'] = proxy.number_of_channels
    return results


@time_it
@restore_state
def test_i2c(proxy):
    '''
    Get metadata for all of the devices on the i2c bus.

    Parameters
    ----------
    proxy : Proxy

    Returns
    -------
    dict
        Nested dictionary (keyed by i2c address) containing metadata
        (e.g., uuid, name, software version, hardwar version) for each of the
        devices on the i2c bus.
    '''
    results = {}
    results['i2c_scan'] = {}
    for address in proxy.i2c_scan():
        if address in [32, 33, 34]:
            node = BaseNode(proxy, int(address))
            info = {'name': node.name().split('\0', 1)[0],
                    'hardware_version':
                    node.hardware_version().split('\0', 1)[0],
                    'software_version':
                    node.software_version().split('\0', 1)[0],
                    'uuid': str(node.uuid)}
            results['i2c_scan'].update({int(address): info})
        elif address == 80:
            n_bytes = proxy.i2c_eeprom_read(80, 0, 1)
            data = proxy.i2c_eeprom_read(80, 1, n_bytes)
            board = metadata.Hardware.FromString(data.tobytes())
            info = {'name': board.name,
                    'hardware_version': board.version,
                    'uuid': str(uuid.UUID(bytes=board.uuid))}
            results['i2c_scan'].update({int(address): info})
        elif address == proxy.config.i2c_address:
            pass
        else:
            results['i2c_scan'].update({int(address): {}})
    return results


@time_it
@restore_state
def test_voltage(proxy, n=5, delay=0.1):
    '''
    Test the measured voltage for a range of target voltages.

    Sweep target output voltage between minimum and maximum voltages and
    calculate error between target and measured output.

    Parameters
    ----------
    proxy : Proxy
    n : int
        Number of voltages to measure between minimum and maximum voltage.
    delay : float
        Seconds between measurements.

    Returns
    -------
    dict
        target_voltage: list
            List of target voltages.
        measured_voltage: list
            List of measured voltages.
        delay : float
            Seconds between measurements.
    '''
    proxy.hv_output_enabled = True
    proxy.hv_output_selected = True
    measured_voltage = []
    input_current = []
    output_current = []

    # need to wait for the voltage to stabilize
    proxy.voltage = proxy.min_waveform_voltage + 5
    time.sleep(0.5)

    input_voltage = proxy.measure_input_voltage()

    target_voltage = np.linspace(proxy.min_waveform_voltage,
                                 proxy.max_waveform_voltage, n + 2)[1:-1]
    for v in target_voltage:
        proxy.voltage = v
        time.sleep(delay)
        measured_voltage.append(proxy.measure_voltage())
        results = proxy.measure_input_current()
        input_current.append(results['rms'])
        results = proxy.measure_output_current()
        output_current.append(results['rms'])

    measured_voltage = np.array(measured_voltage)
    return {'target_voltage': target_voltage,
            'measured_voltage': measured_voltage,
            'input_current': input_current,
            'output_current': output_current,
            'input_voltage': input_voltage,
            'delay': delay}


@time_it
@restore_state
def test_shorts(proxy):
    '''
    Check for electrical channel shorts.

    Parameters
    ----------
    proxy : Proxy

    Returns
    -------
    dict
        shorts : list
            List of shorted channels.

    '''
    return {'shorts': proxy.detect_shorts()}


@time_it
@restore_state
def test_on_board_feedback_calibration(proxy):
    '''
    Measure the on-board feedback capacitors.

    Parameters
    ----------
    proxy : Proxy

    Returns
    -------
    dict
        c : list
            List of measured capacitance values for each of the
            test capacitors.
    '''
    proxy.voltage = 100
    proxy.hv_output_enabled = True
    proxy.hv_output_selected = True
    proxy.state_of_channels = np.zeros(proxy.number_of_channels)

    c = []
    for i in [-1, 0, 1, 2]:
        proxy.select_on_board_test_capacitor(i)
        c.append(proxy.measure_capacitance())

    proxy.select_on_board_test_capacitor(-1)
    return {'c_measured': c}


@time_it
@restore_state
def test_channels(proxy, n_reps=1, test_channels=None, shorts=None):
    '''
    Test all channels using the test board.

    Parameters
    ----------
    proxy : Proxy
    n : int
        Number of reps per channel (default=1).
    test_channels : list
        List of channels to test (default=all channesl).
    shorts : list
        List of channels with shorts (if this is not provided, the device will
        perform a scan for shorts).

    Returns
    -------
    dict
        test_channels : list
            List of channels tested.
        shorts : list
            List of channels with shorts.
        c : np.array
            [m x n] array of measured capacitance values where m
            is the number of channels tested and n is the number
            of reps.
    '''
    n_channels = proxy.number_of_channels
    if not shorts:
        shorts = proxy.detect_shorts()
    if not test_channels:
        test_channels = np.arange(0, n_channels)

    proxy.voltage = 100
    proxy.hv_output_enabled = True
    proxy.hv_output_selected = True

    c = np.zeros([n_channels, n_reps])

    for i, channel_i in enumerate(test_channels):
        for j in range(n_reps):
            if channel_i in shorts:
                continue
            state = np.zeros(n_channels)
            state[channel_i] = 1
            proxy.state_of_channels = state
            c[i, j] = proxy.measure_capacitance()

    return {'test_channels': test_channels,
            'shorts': shorts,
            'c': c}


def self_test(proxy, tests=None):
    '''
    Perform quality control tests.

    Parameters
    ----------
    proxy : dropbot.SerialProxy
        DropBot control board reference.
    tests : list, optional
        List of names of test functions to run.

        By default, run all tests.

    Returns
    -------
    dict
        Results from all tests.
    '''
    total_time = 0

    if tests is None:
        tests = ['system_info',
                 'test_i2c',
                 'test_voltage',
                 'test_shorts',
                 'test_on_board_feedback_calibration',
                 'test_channels']
    results = {}

    for test_name_i in tests:
        start_time_i = time.time()
        test_func_i = eval(test_name_i)
        results[test_name_i] = test_func_i(proxy)
        results[test_name_i]['utc_timestamp'] = (dt.datetime.utcnow()
                                                 .isoformat())
        duration_i = time.time() - start_time_i
        results[test_name_i]['test_duration'] = duration_i
        logger.info('%s: %.1f s', test_name_i, duration_i)
        total_time += duration_i

    logger.info('**Total time: %.1f s**', total_time)

    return results
