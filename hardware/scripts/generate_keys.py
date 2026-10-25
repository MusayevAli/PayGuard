#!/usr/bin/env python3
"""Create a private provisioning bundle and role-specific files; never print keys."""
import json
import os
from pathlib import Path
import secrets
import sys

if len(sys.argv) != 2:
    raise SystemExit("Usage: generate_keys.py /absolute/new/private-directory")
target = Path(sys.argv[1]).resolve()
target.mkdir(mode=0o700, parents=True, exist_ok=False)
keys = {role:secrets.token_hex(32) for role in ("admin","controller","response")}
for name,data in [("keys.json",keys)] + [(role+".json",{role:key}) for role,key in keys.items()]:
    fd = os.open(target/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as f:
        json.dump(data,f,indent=2)
        f.write("\n")
print("Provisioning bundle and three role files created. Keep the bundle off the agent host.")
