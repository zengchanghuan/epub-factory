#!/usr/bin/env python3
import base64
import os
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv(".env")
    raw = (os.environ.get("ALIPAY_PRIVATE_KEY") or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]

    if not raw:
        raise RuntimeError("ALIPAY_PRIVATE_KEY is empty")

    # Normalize to PEM (PKCS8) first.
    if "BEGIN" in raw:
        pem = raw.replace("\\n", "\n")
    else:
        der = base64.b64decode("".join(raw.split()))
        with tempfile.NamedTemporaryFile(delete=False) as der_f:
            der_f.write(der)
            der_path = der_f.name
        with tempfile.NamedTemporaryFile(delete=False) as pem_f:
            pem_path = pem_f.name
        subprocess.run(
            ["openssl", "pkcs8", "-inform", "DER", "-nocrypt", "-in", der_path, "-out", pem_path],
            check=True,
        )
        pem = Path(pem_path).read_text(encoding="utf-8")

    # Convert to PKCS1 DER (rsa lib expects PKCS1 when wrapped as RSA PRIVATE KEY).
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as in_f:
        in_f.write(pem)
        in_path = in_f.name
    with tempfile.NamedTemporaryFile(delete=False) as out_f:
        out_path = out_f.name
    subprocess.run(
        ["openssl", "rsa", "-in", in_path, "-outform", "DER", "-traditional", "-out", out_path],
        check=True,
    )
    pkcs1_b64 = base64.b64encode(Path(out_path).read_bytes()).decode("ascii")

    env_path = Path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out_lines = []
    replaced = False
    for line in lines:
        if line.startswith("ALIPAY_PRIVATE_KEY="):
            out_lines.append("ALIPAY_PRIVATE_KEY=" + pkcs1_b64)
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append("ALIPAY_PRIVATE_KEY=" + pkcs1_b64)
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"ALIPAY_PRIVATE_KEY updated, len={len(pkcs1_b64)}")


if __name__ == "__main__":
    main()
