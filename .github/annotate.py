"""Turn unittest's FAIL/ERROR blocks into GitHub annotations (one ::error line each)."""
import re, sys

text = open(sys.argv[1], errors="replace").read()
blocks = re.findall(r"^(FAIL|ERROR): (.+?)\n-{20,}\n(.*?)(?=^={20,}|^-{20,}\nRan \d+ tests)", text, re.M | re.S)
for kind, name, body in blocks:
    body = "\n".join(body.strip().splitlines()[-25:])
    body = body.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    title = f"{kind} {name.strip()}".replace("%", "%25").replace(":", "%3A").replace(",", "%2C")
    print(f"::error title={title}::{body}")
if not blocks:
    tail = "%0A".join(l.replace("%", "%25") for l in text.strip().splitlines()[-25:])
    print(f"::error title=tests failed::{tail}")
