"""Tiny .properties reader shared by the framework scripts."""
def read_props(*paths):
    out = {}
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().replace("\\,", ",")
    return out
