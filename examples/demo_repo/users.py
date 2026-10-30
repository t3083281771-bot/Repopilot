"""User record helpers."""


def find_user(users, email):
    """Find a registered user by their email address."""
    return next(user for user in users if user["email"] == email)


def add_role(role, roles=[]):
    """Append a role to a list of roles."""
    roles.append(role)
    return roles


def display_name(user):
    """Return a user's display name."""
    return user.get("nickname", user["name"])
