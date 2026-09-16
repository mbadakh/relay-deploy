"""Shared input normalization extracted from the existing deployment process."""
import re,unicodedata
from pathlib import Path
import yaml
class ValidationError(ValueError): pass
def load_yaml(path):
 value=yaml.safe_load(path.read_text())
 if not isinstance(value,dict): raise ValidationError("Expected a mapping")
 return value

def normalize_slug(customer_name: str) -> str:
    if not isinstance(customer_name, str):
        raise ValidationError("customer_name must be a string")
    name = " ".join(customer_name.strip().split())
    if not 2 <= len(name) <= 80 or any(ord(char) < 32 for char in name):
        raise ValidationError("customer_name must contain 2-80 printable characters")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", slug):
        raise ValidationError("customer_name cannot be converted to a safe DNS/Git slug")
    return slug


def normalize_domain(value: str) -> str:
    domain = value.strip().rstrip(".").lower()
    if "://" in domain or "/" in domain or "*" in domain:
        raise ValidationError("domain_name must be a hostname, not a URL or wildcard")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("domain_name is not a valid IDNA hostname") from exc
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253:
        raise ValidationError("domain_name must be a fully-qualified domain name")
    for label in labels:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise ValidationError(f"invalid domain label: {label!r}")
    return domain


