"""Pixel statistics of grabbed frames: is the recurring frame blank/white?"""
from collections import Counter


def stats(path, label):
    data = open(path, "rb").read()
    c = Counter(data[i:i + 4] for i in range(0, len(data), 4 * 89))
    total = sum(c.values())
    top = c.most_common(3)
    print(label, [(k.hex(), round(v / total, 3)) for k, v in top])


stats(r"D:\Code\2026\Browserr\tools\recur_498d3dc6.raw", "recurring A:")
stats(r"D:\Code\2026\Browserr\tools\recur_8ad899f6.raw", "recurring B:")
stats(r"D:\Code\2026\Browserr\tools\other_1.raw", "other 1:")
stats(r"D:\Code\2026\Browserr\tools\other_2.raw", "other 2:")
stats(r"D:\Code\2026\Browserr\tools\other_3.raw", "other 3:")
