"""
Shared rate limiter.

Kept in its own module so route packages can decorate endpoints without
importing the application, which would create an import cycle.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
