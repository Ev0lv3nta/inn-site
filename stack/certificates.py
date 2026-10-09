"""Создать отдельный CA bundle для сайтов с Russian Trusted CA. Систему не меняет."""

import argparse
import hashlib
import ssl
from pathlib import Path
from urllib.request import urlopen

import certifi

CERTIFICATES = {
    "russian_trusted_root_ca_pem.crt": (
        "936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504"
    ),
    "russian_trusted_sub_ca_pem.crt": (
        "f0ae589f36774f29ef3648f7984b08d42fcce6f1ffeeb6236d773daeb2744ea6"
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".cache/russian-ca.pem"))
    args = parser.parse_args()
    chunks = [Path(certifi.where()).read_bytes()]
    for name, expected_hash in CERTIFICATES.items():
        with urlopen("https://gu-st.ru/content/lending/" + name, timeout=20) as response:
            certificate = response.read(10000)
        if hashlib.sha256(certificate).hexdigest() != expected_hash:
            raise RuntimeError("Сертификат изменился: проверьте новый файл на Госуслугах")
        ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
        chunks.append(certificate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(b"\n".join(chunks))
    print(args.output.resolve())


if __name__ == "__main__":
    main()
