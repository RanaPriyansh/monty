# /// monty
# mount-fs = true
# ///
from pathlib import Path

mixed = b'head\r\nbody\rlast\r\r\nend\r'
(root / 'mixed-newlines.txt').write_bytes(mixed)
assert Path(root / 'mixed-newlines.txt').read_text() == 'head\nbody\nlast\n\nend\n'

utf8 = 'café\r\nsecond'.encode('utf-8')
(root / 'utf8-newlines.txt').write_bytes(utf8)
assert Path(root / 'utf8-newlines.txt').read_text() == 'café\nsecond'
