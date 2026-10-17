"""A file read in parts through a function, such as a photo on a camera (PTP reads any
range of a file without sending the rest).

It looks like a file opened for reading (`read`, `seek`, `tell`) so what reads the start
and end of a photo (`capture_time.read_file`, `fingerprint.content_id_of`) works on a
camera as on a card. A read fetches at least `block` bytes from where it is, and what was
fetched last is kept, so the many small reads of a photo's header are one fetch, and a
copy read in order fetches each part once.
"""


class RangeFile:
    def __init__(self, fetch, size, block=64 * 1024):
        """`fetch(offset, length)` returns those bytes of the file: `length` of them
        unless it is cut short by the end, never fewer (an error is raised instead).
        `size` is the file's."""
        self._fetch = fetch
        self.size = size
        self._block = block
        self._position = 0
        # The window kept: where it starts, and its bytes
        self._start = 0
        self._data = b""

    def seek(self, position, whence=0):
        if whence == 1:
            position += self._position
        elif whence == 2:
            position += self.size
        self._position = max(0, position)
        return self._position

    def tell(self):
        return self._position

    def read(self, count=-1):
        """Up to `count` bytes from the position (to the end for -1), fewer at the end."""
        if count is None or count < 0:
            count = self.size - self._position
        count = max(0, min(count, self.size - self._position))
        if count == 0:
            return b""
        end = self._position + count
        if not (self._start <= self._position and end <= self._start + len(self._data)):
            length = min(max(count, self._block), self.size - self._position)
            data = self._fetch(self._position, length)
            if len(data) < count:
                raise OSError(f"the file ends at {self._position + len(data)}, not {self.size}")
            self._start, self._data = self._position, data
        begin = self._position - self._start
        self._position = end
        return self._data[begin : begin + count]
