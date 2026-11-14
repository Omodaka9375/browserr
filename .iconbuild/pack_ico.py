import struct

sizes = [16, 24, 32, 48, 64, 128, 256]
blobs = []
for s in sizes:
    with open(f"icon-{s}.png", "rb") as f:
        blobs.append((s, f.read()))

# ICO: ICONDIR + N * ICONDIRENTRY + image blobs (PNG-compressed entries,
# valid on Vista and later for every size).
out = struct.pack("<HHH", 0, 1, len(blobs))
offset = 6 + 16 * len(blobs)
for s, b in blobs:
    out += struct.pack("<BBBBHHII", 0 if s == 256 else s, 0 if s == 256 else s, 0, 0, 1, 32, len(b), offset)
    offset += len(b)
for _, b in blobs:
    out += b

with open("../engine/assets/browserr.ico", "wb") as f:
    f.write(out)
print(f"browserr.ico {len(out)} bytes, {len(blobs)} entries")
