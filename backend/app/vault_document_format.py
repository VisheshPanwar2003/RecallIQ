"""V1 authenticated PDF container. Only ciphertext may be persisted."""
import struct
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_PDF_BYTES = 20 * 1024 * 1024
HEADER = struct.Struct('>8sB16s16s12sQ')
MAGIC = b'RIQPDF\x00\x00'
MAX_CONTAINER_BYTES = MAX_PDF_BYTES + HEADER.size + 16


class EncryptedDocumentError(RuntimeError):
    def __init__(self):
        super().__init__('Encrypted document operation failed; recovery may be required.')


def document_id(value):
    try:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError()
        return value
    except Exception:
        raise EncryptedDocumentError() from None


def inspect_container(data, vault_id, doc_id):
    """Bound allocation and identities before decryption; this is NOT authentication."""
    try:
        document_id(doc_id)
        if type(data) is not bytes or not HEADER.size + 16 <= len(data) <= MAX_CONTAINER_BYTES:
            raise ValueError()
        magic, version, vault, doc, nonce, size = HEADER.unpack_from(data)
        if magic != MAGIC or version != 1 or vault != UUID(vault_id).bytes or doc != UUID(doc_id).bytes:
            raise ValueError()
        if not 1 <= size <= MAX_PDF_BYTES or len(data) != HEADER.size + size + 16:
            raise ValueError()
        return nonce
    except Exception:
        raise EncryptedDocumentError() from None


def seal_pdf(key, vault_id, doc_id, nonce, data):
    try:
        document_id(doc_id)
        if len(key) != 32 or len(nonce) != 12 or type(data) is not bytes or not 1 <= len(data) <= MAX_PDF_BYTES:
            raise ValueError()
        header = HEADER.pack(MAGIC, 1, UUID(vault_id).bytes, UUID(doc_id).bytes, nonce, len(data))
        return header + AESGCM(key).encrypt(nonce, data, header)
    except Exception:
        raise EncryptedDocumentError() from None


def open_pdf(key, vault_id, doc_id, data):
    try:
        nonce = inspect_container(data, vault_id, doc_id)
        if len(key) != 32:
            raise ValueError()
        return AESGCM(key).decrypt(nonce, data[HEADER.size:], data[:HEADER.size])
    except Exception:
        raise EncryptedDocumentError() from None
