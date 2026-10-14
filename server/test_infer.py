"""infer.py without its model files (none in CI): the CMVN reader, and check() naming every missing or different file."""
import struct

import numpy as np
import pytest

import infer


def test_cmvn_and_check(tmp_path):
    stats = np.array([[2.0, 4.0, 2.0], [6.0, 20.0, 0.0]])  # count 2: means 1, 2; variances 2, 6
    (tmp_path / infer.CMVN).write_bytes(b"\0BDM \4" + struct.pack("<i", 2) + b"\4" + struct.pack("<i", 3) + stats.astype("<f8").tobytes())
    mean, istd = infer.cmvn(tmp_path / infer.CMVN)
    assert np.allclose(mean, [1, 2]) and np.allclose(istd, [1 / np.sqrt(2), 1 / np.sqrt(6)])
    with pytest.raises(SystemExit) as e:
        infer.check(tmp_path)
    assert all(n in str(e.value) for n in infer.MODELS) and "another sha256" in str(e.value) and "is missing" in str(e.value)
