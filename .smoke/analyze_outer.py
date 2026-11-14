import re
data = open('/mnt/d/Code/2026/Browserr/.smoke/tmux_outer.bin', 'rb').read()
print('literal Ptmux:', data.count(b'Ptmux'))
print('doubled ESC Ptmux:', len(re.findall(rb'\x1b\x1bPtmux;', data)))
print('doubled sixel head:', len(re.findall(rb'\x1b\x1bP7;1;0q', data)))
print('plain sixel head:', len(re.findall(rb'\x1bP7;1;0q', data)))
m = re.search(rb'\x1b\x1bP7;1;0q"1;1;(\d+);(\d+)', data)
print('first image raster:', m.groups() if m else None)
rows = [int(x) for x in re.findall(rb'\x1b\x1b\[(\d+);\d+H', data)]
print('inner cursor rows max:', max(rows) if rows else None)
