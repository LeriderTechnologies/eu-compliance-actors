"""RFC 8017 interoperability: RSA PKCS#1 v1.5 signatures whose DigestInfo omits the (optional) NULL parameters.

RFC 8017 § 9.2 note 1 says both DigestInfo encodings of the SHA-2 AlgorithmIdentifier (parameters NULL or absent)
should be accepted. pyca/cryptography, which pyHanko uses, only accepts the NULL form, so valid seals from some
qualified providers fail as "cryptographically invalid": found 2026-10-28 on the Austrian Federal Chancellery's
A-Trust seal on BGBl authentic PDFs.

`install()` wraps pyhanko_certvalidator's raw verification: if the strict check fails for an RSA PKCS#1 v1.5
signature, recover the DigestInfo from the signature and accept only if it is exactly the parameters-absent
encoding for the same hash algorithm and the same digest. Every other failure is re-raised unchanged.
"""
from __future__ import annotations

import hashlib

from asn1crypto import algos
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pyhanko_certvalidator import sig_validate

_HASHES = {"sha256": hashlib.sha256, "sha384": hashlib.sha384, "sha512": hashlib.sha512, "sha224": hashlib.sha224}
_installed = False


def _params_absent_match(signature: bytes, signed_data: bytes, public_key_info, signature_algorithm,
                         context) -> bool:
    try:
        sig_algo = signature_algorithm.signature_algo
    except ValueError:
        sig_algo = signature_algorithm["algorithm"].native
    if sig_algo != "rsassa_pkcs1v15":
        return False
    try:
        hash_name = signature_algorithm.hash_algo
    except ValueError:  # plain rsassa_pkcs1v15: the hash comes from the CMS digest algorithm, as in pyHanko
        hash_name = getattr(context, "contextual_md_algorithm", None)
    prehashed = bool(getattr(context, "prehashed", False))
    if hash_name not in _HASHES:
        return False
    pub = serialization.load_der_public_key(public_key_info.dump())
    if not isinstance(pub, rsa.RSAPublicKey):
        return False
    try:
        recovered = pub.recover_data_from_signature(signature, padding.PKCS1v15(), None)
        info = algos.DigestInfo.load(recovered, strict=True)
    except (InvalidSignature, ValueError):
        return False
    alg = info["digest_algorithm"]
    digest = signed_data if prehashed else _HASHES[hash_name](signed_data).digest()
    return (alg["algorithm"].native == hash_name and alg["parameters"].native is None
            and not alg["parameters"].contents and info["digest"].native == digest
            and recovered == info.dump())  # canonical DER only, no trailing bytes


def install() -> None:
    global _installed
    if _installed:
        return
    original = sig_validate._validate_raw

    def _validate_raw(signature, signed_data, public_key_info, signature_algorithm,
                      context=sig_validate.DEFAULT_VALIDATION_CONTEXT):
        try:
            return original(signature, signed_data, public_key_info, signature_algorithm, context)
        except InvalidSignature:
            if _params_absent_match(signature, signed_data, public_key_info, signature_algorithm, context):
                return None
            raise

    sig_validate._validate_raw = _validate_raw
    _installed = True
