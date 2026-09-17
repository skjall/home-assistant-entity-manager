"""Cleaning and validating what arrives from a client.

Names, entity ids and registry ids reach the add-on from the browser and from
tools; they are cut to length, stripped of control characters and checked
against Home Assistant's own shapes before anything is done with them.
"""

import re
import unicodedata

# Maximum lengths for different input types
MAX_NAME_LENGTH = 255
MAX_ENTITY_ID_LENGTH = 255
MAX_REGISTRY_ID_LENGTH = 64

# Valid characters for entity IDs (Home Assistant format: domain.object_id)
ENTITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

# Valid characters for registry IDs (typically alphanumeric with some special chars)
REGISTRY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_string(value: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """
    Sanitize a general string input.
    - Strips whitespace
    - Removes control characters
    - Escapes HTML entities
    - Limits length
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)

    # Strip whitespace
    value = value.strip()

    # Remove control characters (keep newlines and tabs for multi-line text)
    value = "".join(char for char in value if unicodedata.category(char) != "Cc" or char in "\n\t")

    # Remove null bytes and other dangerous characters
    value = value.replace("\x00", "")

    # Limit length
    value = value[:max_length]

    return value


def sanitize_name(value: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """Clean a display name that is about to be stored.

    A name goes into Home Assistant's registry and comes back out through its
    API - it is data, not markup, and it is kept exactly as the user wrote it.
    An apostrophe stays an apostrophe: a coffee machine's "Calc'n'Clean in 5
    Tassen" used to be stored as "Calc&#x27;n&#x27;Clean in 5 Tassen" and read
    that way everywhere afterwards, in Home Assistant itself and in every
    automation that showed the name.

    Escaping belongs where a name is rendered, and that is already handled:
    Alpine's x-text and Jinja's autoescaping put text in as text, and the one
    place that builds markup - the diff between two names - escapes every
    piece it inserts.
    """
    return sanitize_string(value, max_length)


def sanitize_entity_id(value: str) -> str:
    """
    Sanitize and validate an entity ID.
    Entity IDs must be lowercase, alphanumeric with underscores, in format domain.object_id
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None

    # Strip and lowercase
    value = value.strip().lower()

    # Limit length
    value = value[:MAX_ENTITY_ID_LENGTH]

    # Replace spaces and hyphens with underscores
    value = value.replace(" ", "_").replace("-", "_")

    # Remove any characters that aren't valid
    value = re.sub(r"[^a-z0-9_.]", "", value)

    # Validate format
    if not ENTITY_ID_PATTERN.match(value):
        return None

    return value


def sanitize_registry_id(value: str) -> str:
    """
    Sanitize and validate a registry ID.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None

    # Strip whitespace
    value = value.strip()

    # Limit length
    value = value[:MAX_REGISTRY_ID_LENGTH]

    # Validate format (alphanumeric, underscore, hyphen)
    if not REGISTRY_ID_PATTERN.match(value):
        return None

    return value


def validate_json_input(data: dict, required_fields: list = None) -> tuple:
    """
    Validate that JSON input is a dict and has required fields.
    Returns (is_valid, error_message)
    """
    if not isinstance(data, dict):
        return False, "Invalid JSON input"

    if required_fields:
        missing = [f for f in required_fields if f not in data]
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"

    return True, None
