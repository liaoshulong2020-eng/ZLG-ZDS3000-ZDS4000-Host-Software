"""Bounded display arrays preserving extrema and their original sample times."""
import numpy as np
from .protocol import raw_code_to_voltage


class DisplayEnvelope:
    def __init__(self, data):
        self.data = data
        self.levels = []
        self.raw_values = data._voltage_array is None
        values = data.raw_data if self.raw_values else data._voltage_array
        self.values = values
        n = len(values)
        # Build small extrema indexes once; never materialize full time/voltage arrays.
        if n > 20000:
            parts = []
            for start in range(0, n, 1048576):
                end = min(n, start + 1048576)
                parts.append(self._reduce(np.arange(start, end), 256))
            indexes = np.concatenate(parts)
            block = 256
            self.levels.append((block, indexes))
            while len(indexes) > 20000:
                indexes = self._reduce(indexes, 256)
                block *= 128
                self.levels.append((block, indexes))

    def _reduce(self, indexes, group):
        count = len(indexes) // group
        if not count:
            return indexes
        rows = indexes[:count * group].reshape(count, group)
        values = self.values[rows]
        r = np.arange(count)
        result = np.sort(np.column_stack((rows[r, values.argmin(axis=1)],
                                         rows[r, values.argmax(axis=1)])).ravel())
        return np.concatenate((result, indexes[count * group:]))

    def samples(self, left=None, right=None, pixels=1500):
        data = self.data
        n = len(self.values)
        if not n:
            return np.array([]), np.array([])
        if data._time_array is not None:
            a = 0 if left is None else max(0, int(np.searchsorted(data._time_array, left))-1)
            b = n if right is None else min(n, int(np.searchsorted(data._time_array, right))+1)
        else:
            a = 0 if left is None else max(0, min(n, int(np.floor((left-data.start_time)/data.dt))-1))
            b = n if right is None else max(0, min(n, int(np.ceil((right-data.start_time)/data.dt))+2))
        budget = max(100, int(pixels)) * 4
        chosen = None
        for block, indexes in self.levels:
            if (b-a)/block >= pixels:
                chosen = block, indexes
        if chosen:
            block, indexes = chosen
            # Boundary blocks are read raw so offscreen extrema cannot hide visible ones.
            lo = min(b, ((a+block-1)//block)*block)
            hi = max(lo, (b//block)*block)
            middle = indexes[np.searchsorted(indexes, lo):np.searchsorted(indexes, hi)]
            idx = np.concatenate((np.arange(a, lo), middle, np.arange(hi, b)))
        else:
            idx = np.arange(a, b)
        if len(idx) > budget:
            idx = self._reduce(idx, max(2, int(np.ceil(len(idx)/(budget/2)))))
        if b > a:
            idx = np.unique(np.concatenate(([a], idx, [b-1])))
        times = data._time_array[idx] if data._time_array is not None else data.start_time + idx*data.dt
        values = self.values[idx]
        if self.raw_values:
            i = data.info
            values = raw_code_to_voltage(values, i.vert_division, i.vert_offset, i.probe_att, i.reversed)
        return times, values
