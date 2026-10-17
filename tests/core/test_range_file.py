"""A file read in parts through a function (a photo on a camera)."""

import pytest

from lumina.core.range_file import RangeFile


class Source:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def fetch(self, offset, length):
        self.calls.append((offset, length))
        return self.data[offset : offset + length]


def test_it_reads_as_a_file_does():
    data = bytes(range(256)) * 1000
    source = Source(data)
    file = RangeFile(source.fetch, len(data))

    assert file.read(10) == data[:10]
    file.seek(1000)
    assert file.read(5) == data[1000:1005]
    assert file.tell() == 1005
    file.seek(-4, 2)
    assert file.read(100) == data[-4:]
    assert file.read(1) == b""
    file.seek(0)
    assert file.read() == data


def test_small_reads_are_one_fetch_of_a_block():
    source = Source(bytes(1_000_000))
    file = RangeFile(source.fetch, 1_000_000, block=64 * 1024)

    file.read(16)
    file.seek(100)
    file.read(2000)
    file.seek(70_000)
    file.read(4000)

    assert source.calls == [(0, 65536), (70_000, 65536)]


def test_a_fetch_is_not_asked_past_the_end():
    source = Source(bytes(100))
    RangeFile(source.fetch, 100).read(10)

    assert source.calls == [(0, 100)]


def test_a_short_fetch_is_an_error():
    source = Source(bytes(50))
    file = RangeFile(source.fetch, 100)

    with pytest.raises(OSError, match="ends at 50"):
        file.read(80)
