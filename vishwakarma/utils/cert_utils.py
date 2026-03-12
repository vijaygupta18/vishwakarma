"""
Custom CA certificate injection.
MUST be imported before any HTTPS client is initialized.
"""
import logging
import os
import ssl
import tempfile


def inject_custom_cert(cert_pem: str) -> bool:
    """
    Inject a custom CA certificate into the default SSL context.
    Call this before importing httpx, requests, openai, litellm, etc.
    Returns True if certificate was injected.
    """
    if not cert_pem:
        return False

    try:
        # Write cert to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False
        ) as f:
            f.write(cert_pem)
            cert_path = f.name

        # Point SSL env vars to our cert bundle
        os.environ["SSL_CERT_FILE"] = cert_path
        os.environ["REQUESTS_CA_BUNDLE"] = cert_path
        os.environ["CURL_CA_BUNDLE"] = cert_path

        # Validate cert is parseable
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cert_path)

        logging.info(f"Custom CA certificate injected from env CERTIFICATE")
        return True

    except Exception as e:
        logging.error(f"Failed to inject custom certificate: {e}")
        return False


# Auto-inject on import if CERTIFICATE env var is set
_cert = os.environ.get("CERTIFICATE", "")
if _cert:
    inject_custom_cert(_cert)
