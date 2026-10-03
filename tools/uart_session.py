"""Bounded UART baud recovery for board runners; never resets the board."""
from contextlib import contextmanager
import sys


@contextmanager
def restoring_baud(rate, default=115200):
    """Keep cleanup active before the first switch, including ambiguous failures.

    ``rate(before, after, label)`` must close its link, retain its receipt and
    raise unless status confirms the destination baud. A failed switch can
    leave either endpoint active, so recovery probes both, destination first.
    """
    candidates = [default]

    def switch(before, after, label):
        candidates[:] = list(dict.fromkeys([after, before, *candidates]))
        rate(before, after, label)
        candidates[:] = [after]

    try:
        yield switch
    finally:
        original = sys.exc_info()[1]
        failures = []
        for index, baud in enumerate(candidates):
            label = 'uart-restored' if index == 0 else f'uart-restored-from-{baud}'
            try:
                rate(baud, default, label)
                break
            except Exception as error:
                failures.append(f'{baud}: {type(error).__name__}: {error}')
        else:
            message = 'UART restoration failed; baud is unconfirmed: ' + '; '.join(failures)
            if original is None:
                raise RuntimeError(message)
            # Keep the upload/capture/interrupt failure as the primary error.
            # All individual recovery attempts retain their own rate receipts.
            print(message, file=sys.stderr)
