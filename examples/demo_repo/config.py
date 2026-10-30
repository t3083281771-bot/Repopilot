"""Environment variable conversion and URL composition."""


def parse_bool(value):
    """Convert an environment variable to a boolean."""
    return bool(value)


def build_url(base, path):
    """Join the service base URL and a relative request path."""
    return base + "/" + path


def load_timeout(environment):
    """Load a request timeout in seconds."""
    return int(environment.get("TIMEOUT", "30"))
