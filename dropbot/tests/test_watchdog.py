import logging
import time

import pytest
import serial
from six.moves import range
from dropbot.proxy import SerialProxy, NoPower, I2cAddressNotSet

# Watchdog enable bit mask
WDOG_STCTRLH_WDOGEN = 0x01


@pytest.fixture(autouse=True)
def restore_watchdog_time_out(proxy):
    WDOG_STCTRLH = proxy.R_WDOG_STCTRLH()
    watchdog_enabled = (WDOG_STCTRLH & WDOG_STCTRLH_WDOGEN)

    if watchdog_enabled:
        # Save initial watchdog time out.
        initial_time_out = proxy.watchdog_time_out_value()
        print('Initial watchdog time out:', initial_time_out)

    yield

    if watchdog_enabled:
        # Restore initial watchdog time out.
        proxy.watchdog_enable(0, initial_time_out)
        time.sleep(0.1)


@pytest.fixture(scope='module')
def proxy():
    '''
    .. versionchanged:: 1.27.1
        Ignore non-critical exceptions during initialization to allow tests to
        run on standalone control board hardware (i.e., not connected to power
        or I2C bus).
    '''


    # XXX Ignore non-critical exceptions during initialization.
    proxy_ = SerialProxy(ignore=[NoPower,
                                    I2cAddressNotSet])
    yield proxy_
    proxy_.terminate()


@pytest.mark.parametrize('retry_count', [1, 5])
def test_disable(proxy, retry_count):
    # Reboot to reach known state.
    proxy.reboot()

    # XXX Executing `watchdog_disable` method results in undefined behaviour.
    # XXX Rebooting and retrying to disable the watchdog seems to help.
    # Empirically, disable test seems to pass on attempt 1-3.
    #
    # See [issue 4][i4].
    #
    # [i4]: https://gitlab.com/sci-bots/dropbot.py/issues/4
    for i in range(retry_count):
        proxy.watchdog_disable()
        timer_output = proxy.watchdog_timer_output()
        if timer_output == 0:
            print(f'Disabled on attempt {i + 1}')
            break
        else:
            # Disabling watchdog was not successful. Reboot and try again.
            proxy.reboot()
    else:
        assert timer_output == 0

    WDOG_STCTRLH = proxy.R_WDOG_STCTRLH()
    assert not (WDOG_STCTRLH & WDOG_STCTRLH_WDOGEN)


@pytest.mark.parametrize('time_out', [2000, 5000])
def test_enable(proxy, time_out):
    # Set watchdog time out.
    proxy.watchdog_enable(0, time_out)

    # Disable watchdog refresh to trigger time out.
    proxy.watchdog_auto_refresh(False)

    output_count = (time_out - 100) // 10
    print(f'Testing {output_count} times')
    for _ in range(output_count):
        timer_output_i = proxy.watchdog_timer_output()
        assert timer_output_i <= time_out
        assert timer_output_i >= 0
    print('Timer output:', timer_output_i)

    proxy.watchdog_auto_refresh(True)


def test_time_out(proxy):
    start_reset_count = proxy.watchdog_reset_count()

    # Set watchdog time out to 2 seconds.
    proxy.watchdog_enable(0, 2000)

    # Disable watchdog refresh to trigger time out.
    proxy.watchdog_auto_refresh(False)

    # Wait for watchdog time out.
    time.sleep(2)

    # Verify that serial connection is lost due to time out.
    with pytest.raises(serial.SerialException):
        proxy.ram_free()

    # Explicitly tear down proxy stream state.
    proxy.terminate()

    # Wait for serial port to settle after device reset due to watchdog time out.
    time.sleep(1)

    # Re-establish serial connection to the device.
    proxy._connect()

    # Verify that watchdog reset actually occurred.
    end_reset_count = proxy.watchdog_reset_count()
    proxy.watchdog_reset_count_clear(0xffff)
    assert end_reset_count > start_reset_count
